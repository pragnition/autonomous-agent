# Rust Control Plane, Stage 2: Runtime — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Rust code that runs one prepared turn in its conversation's container, end to end: the holder, the containers, the turn logic ported from `src/agent/runner/claude.py`, transcripts in today's format, and reconnecting after `agentd` restarts.

**Architecture:** A Cargo workspace under `rust/` with three crates: `holder-proto` (the socket and log types shared by both programs), `agent-holder` (the binary that runs inside every conversation container: `serve`, `hook idle-wait`, `mcp`) and `agentd` (library plus binary; this stage adds its database layer, container manager and turn runner). The turn runner reads Claude Code's output from the holder's numbered log on the shared volume and controls the turn through the holder's socket; its whole state is rebuilt by replaying that log, which is what makes reconnecting after a restart exact. Stage 3 feeds it work from the queue.

**Tech Stack:** Rust 1.100 (`rust:1.100-bookworm` dev image), tokio, serde/serde_json, rusqlite (bundled), bollard, reqwest, regex, nix, tracing, anyhow/thiserror.

**Spec:** `docs/superpowers/specs/2026-11-22-rust-control-plane-design.md`; spike results: `docs/superpowers/specs/2026-11-22-rust-control-plane-spike.md`.

## Global Constraints

All of the master plan's (`docs/superpowers/plans/2026-11-23-rust-control-plane.md`), plus:
- Claude Code is started with: `--output-format stream-json --verbose --input-format stream-json --replay-user-messages --permission-mode bypassPermissions --setting-sources user,project`, `--model`, `--system-prompt`, `--settings <json>`, `--mcp-config <json>`, `--plugin-dir <dir>` per plugin, and `--session-id <uuid>` (fresh) or `--resume=<id>`; then an `initialize` control request and the prompt on stdin.
- Environment for Claude Code, rebuilt every turn: secret-looking variables of `agentd`'s own environment blanked (the runner's `SECRETISH` regex), then `agent.env`, then `data/runner/linear.env`, then the project's test secrets, then forced: `ANTHROPIC_BASE_URL=http://172.30.0.1:28091`, `CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-container-via-host-proxy`, `ENABLE_CLAUDEAI_MCP_SERVERS=false`, `CLAUDE_CONFIG_DIR=/var/agent/claude`, `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1`, `AGENT_WORK_ITEM`, `AGENT_CONVERSATION`, `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, `SOFT_CAP_5H`, `SOFT_CAP_LIFT_S`.
- Notes copied verbatim from `src/agent/runner/worker.py`: `FRESH_NOTE`, `MIDTURN_NOTE`, `RESTARTED_NOTE`, `MEMORY_RULES`; the handoff note from `src/agent/runner/handoff.py`; `IDLE_WAIT_REASON` from `src/agent/runner/idle_wait.py`.
- Turn end, from the runner and the spike: a `result` ends the turn only when the prompt and every sent message have been echoed, no background agent (`task_type` `local_agent` or `local_workflow`) is running, and no shell moved by a `now` message (`tool_use_result.backgroundedToDeliverMessage`) is still running; otherwise it is interim and its text is kept.
- Account moves: at once on `rejected`/`disabled`; over the soft cap, at the next quiet moment (no tool call in flight, nothing in the background) when another account has room, or after 30 minutes over the cap; at most 2 per turn. A session is never resumed on another account.
- Container per conversation: image from config (default `autonomous-agent-agent`), `--init`, `network_mode: container:agent-docker`, the agent service's mounts, memory 8 GiB, 4 CPUs, 4096 pids by default (`[containers]` in `agent.toml`); idle stop after 60 minutes with no turn, no listener and no other process; 24 hours after the last turn at most; deleted 7 days after stopping.

## Review Focus

- `agentd` restarts while a turn is ending: the result must be reported exactly once — `turn::tests::restart_after_result_reports_once`.
- Two `start` requests for one conversation race while its container starts: one Claude Code only — `containers::tests::ensure_is_serialised_per_conversation`.
- A tool call in flight when `agentd` restarts: it runs once and its stored result is sent again — `turn::tests::tool_call_replayed_after_restart_runs_once`.
- A holder log line longer than 1 MiB (a screenshot in a tool result): read whole, never split — `holder::tests::long_line_kept_whole`.
- Holder logs and stopped containers piling up: old logs and containers are removed — `containers::tests::sweep_deletes_week_old_containers_and_logs`.

---

### Task 1: Workspace, dev image and the shared protocol

**Files:**
- Create: `rust/Cargo.toml` (workspace: `crates/holder-proto`, `crates/agent-holder`, `crates/agentd`), `rust/rust-toolchain.toml` (`1.100`), `rust/crates/holder-proto/{Cargo.toml,src/lib.rs}`
- Modify: `Dockerfile` (stage `rust-dev` from `rust:1.100-bookworm`), `justfile` (recipe `rust-test`: `cargo test --workspace` in that stage, with the cargo registry and `target/` cached in a named volume)
- Test: `rust/crates/holder-proto/src/lib.rs` (unit tests)

**Interfaces:**
- Produces, in `holder-proto`:
  - `enum Request { Start { work_item: String, argv: Vec<String>, env: BTreeMap<String,String>, cwd: String, stdin: Vec<serde_json::Value>, tools: Vec<ToolSpec> }, Send { line: serde_json::Value }, Stop { grace_s: u64 }, Status, ToolResult { id: u64, text: String }, Tools, Tool { name: String, args: serde_json::Value } }` (serde tag `op`, snake case)
  - `enum Response { Ok, Started { pid: u32 }, Status(Status), Tools { tools: Vec<ToolSpec> }, ToolText { text: String }, Error { message: String } }`
  - `struct Status { work_item: Option<String>, pid: Option<u32>, alive: bool, listeners: Vec<u16>, other_processes: u32, log_seq: u64 }`
  - `enum Entry { Started { pid: u32 }, Out { line: String }, Sent { line: serde_json::Value }, Tool { id: u64, name: String, args: serde_json::Value }, Exit { code: Option<i32>, signal: Option<i32> } }` written as `{"seq":N,"kind":...,...}` by `struct LogLine { seq: u64, entry: Entry }`
  - `struct ToolSpec { name: String, description: String, input_schema: serde_json::Value }`
  - `fn run_dir(conversation: &str) -> PathBuf` = `/var/agent/run/containers/<conversation>`; `fn log_path(conversation, work_item)`; `fn socket_path(conversation)`

- [ ] **Step 1: Write failing tests** `request_round_trips_as_tagged_json` (`{"op":"send","line":{...}}`), `log_line_round_trips` (each `Entry` variant), `run_dir_rejects_path_separators` (a conversation id with `/` or `..` panics or errors — ids are generated by core, but never trusted as paths).
- [ ] **Step 2: Run** `just rust-test` — Expected: FAIL (types missing).
- [ ] **Step 3: Implement the types and path helpers.**
- [ ] **Step 4: Run** `just rust-test` — Expected: PASS.
- [ ] **Step 5: Commit** `rust: workspace, dev image and the holder protocol`.

### Task 2: `agent-holder serve`

**Files:**
- Create: `rust/crates/agent-holder/{Cargo.toml,src/main.rs,src/serve.rs,src/listeners.rs}`, `rust/crates/agent-holder/tests/serve.rs`, `rust/crates/agent-holder/tests/fake_claude.rs` (a test binary target `fake-claude` that plays a scenario file: each line is `{"after_stdin": <n>, "emit": {...}}` or `{"sleep_ms": n}` or `{"exit": code}`, echoing user lines that carry a `uuid` with `isReplay: true` as the real CLI does)

**Interfaces:**
- Consumes: `holder-proto`.
- Produces: `agent-holder serve --conversation <id>` — the container's main process. It owns the run directory, writes the log, answers requests on the socket, starts at most one Claude Code at a time, appends `Sent` before writing a line to stdin, appends `Exit` when the process ends, and keeps a pending `Tool` call open until `ToolResult` arrives. `listeners.rs`: `fn listeners_owned_here() -> Vec<u16>` (the spike's `/proc` method).

- [ ] **Step 1: Write failing tests** in `tests/serve.rs`, each against a real `serve` on a temp dir with `fake-claude`:
  - `start_writes_numbered_log_and_exit` — seqs 1..N contiguous; `Started`, `Out`…, `Exit{code:Some(0)}`.
  - `send_is_logged_before_written` — a `Sent` entry precedes the echo `Out` of the same uuid.
  - `second_start_while_running_is_refused` — `Error`.
  - `log_survives_holder_restart_and_numbering_continues` — restart `serve`; next entry is `last+1`.
  - `long_line_kept_whole` — a 3 MiB `Out` line round-trips intact.
  - `tool_call_waits_for_result` — `Tool{name,args}` request blocks until `ToolResult{id}` and returns its text; the log has the `Tool` entry.
  - `stop_terminates_process_group` — `Stop{grace_s:1}` ends fake-claude and a child it spawned.
  - `status_reports_listener_in_this_container_only` — a listener started by the test shows in `listeners`.
- [ ] **Step 2: Run** `just rust-test` — Expected: FAIL.
- [ ] **Step 3: Implement** `serve` with tokio (`UnixListener`, child with its own process group via `nix::unistd::setpgid` in `pre_exec`), lines read with no size limit.
- [ ] **Step 4: Run** `just rust-test` — Expected: PASS.
- [ ] **Step 5: Commit** `agent-holder: serve`.

### Task 3: `agent-holder hook idle-wait` and `agent-holder mcp`

**Files:**
- Create: `rust/crates/agent-holder/src/idle_wait.rs`, `rust/crates/agent-holder/src/mcp.rs`
- Test: unit tests in `idle_wait.rs` (every case in `tests/test_idle_wait.py`, same inputs and verdicts); `rust/crates/agent-holder/tests/mcp.rs`

**Interfaces:**
- Produces: `fn idle_wait(command: &str) -> bool` (a port of `src/agent/runner/idle_wait.py`: heredoc bodies dropped, quoted strings taken out, `sleep`/`read -t` over 10 s in any command position, `tail -f /dev/null`, `python -c` with `time.sleep`, `sh -c` scripts and `$(...)` inside double quotes checked recursively); `agent-holder hook idle-wait` reads the hook event from stdin and prints the deny JSON with `IDLE_WAIT_REASON` when `tool_name == "Bash"`, not `run_in_background`, and `idle_wait(command)`.
- Produces: `agent-holder mcp --conversation <id>` — a stdio MCP server (newline-delimited JSON-RPC: `initialize`, `tools/list` from the holder's `Tools`, `tools/call` relayed as `Tool` and answered with `{"content":[{"type":"text","text":...}]}`).

- [ ] **Step 1: Write failing tests**: the ported `idle_wait` cases; `hook_denies_waiting_bash_and_allows_others` (runs the binary with event JSON on stdin); `mcp_lists_tools_and_relays_call` (a `serve` with tools set by `Start`; the MCP stub answers `tools/list` and relays `tools/call` to a `ToolResult` the test sends).
- [ ] **Step 2: Run** `just rust-test` — Expected: FAIL.
- [ ] **Step 3: Implement both subcommands.**
- [ ] **Step 4: Run** `just rust-test` — Expected: PASS.
- [ ] **Step 5: Commit** `agent-holder: idle-wait hook and MCP relay`.

### Task 4: `agentd` config and database

**Files:**
- Create: `rust/crates/agentd/{Cargo.toml,src/lib.rs,src/main.rs,src/config.rs,src/db.rs}`, `rust/crates/agentd/src/schema.sql` (core's `SCHEMA` from `src/agent/db.py`, verbatim, plus the new tables below)
- Test: unit tests in `config.rs` and `db.rs`

**Interfaces:**
- Produces: `struct Config { name, timezone, people, limits: Limits { max_concurrent_sessions, max_tasks_per_day, idle_minutes, schedule_hours }, claude_models, containers: Containers { image, memory_gib: 8, cpus: 4.0, pids: 4096, idle_stop_minutes: 60, max_age_hours: 24, keep_stopped_days: 7, min_free_gib: 3 }, data_dir, ... }` with `fn load(path: &Path) -> Result<Config, Vec<String>>` returning every problem, like `src/agent/config.py` (only the fields this stage uses; stage 3 adds the rest).
- Produces: `struct Db` (one `rusqlite::Connection`, WAL, busy timeout 5 s, used through `async fn call<T>(&self, f: impl FnOnce(&Connection) -> Result<T> + Send) -> Result<T>` on a blocking thread). New tables: `agentd_containers(conversation_id TEXT PRIMARY KEY, name TEXT NOT NULL, image_id TEXT, created_at TEXT NOT NULL, last_turn_at TEXT, stopped_at TEXT, replace INTEGER NOT NULL DEFAULT 0)`, `agentd_turns(work_item_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, state TEXT NOT NULL, forwarded_seq INTEGER NOT NULL DEFAULT 0, result TEXT, started_at TEXT NOT NULL, ended_at TEXT)`, `agentd_tool_calls(work_item_id TEXT NOT NULL, call_id INTEGER NOT NULL, result TEXT NOT NULL, PRIMARY KEY(work_item_id, call_id))`.

- [ ] **Step 1: Write failing tests**: `schema_matches_python_core` (the tables and columns of a DB created by `Db::open` equal those of a DB created by `agent.db` — fixture `rust/crates/agentd/tests/fixtures/core_schema.txt` generated by `python -c` in the Python dev image), `open_existing_db_keeps_rows`, `config_reports_every_problem` (two bad fields → two problems), `container_defaults`.
- [ ] **Step 2: Run** `just rust-test` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `just rust-test` — Expected: PASS.
- [ ] **Step 5: Commit** `agentd: config and database`.

### Task 5: Records in today's transcript format

**Files:**
- Create: `rust/crates/agentd/src/records.rs`, `rust/crates/agentd/tests/fixtures/records/{input.jsonl,expected.jsonl}` (generated: raw stream-json lines covering user, echo, assistant with text/thinking/tool_use, tool_result, system init, task_started/updated/notification/progress, result success and error, rate_limit_event, stream_event; expected = `{"type": type(msg).__name__, **dataclasses.asdict(msg)}` from the Python SDK 0.2.180 parser, as `src/agent/runner/claude.py::_append` writes it)

**Interfaces:**
- Produces: `fn to_record(line: &serde_json::Value) -> serde_json::Value` (the SDK class name and fields; unknown types as the SDK leaves them), `fn truncate_for_event(record) -> Value` (over 20,000 characters: `{"type", "truncated"}` as `worker._forwarder`), and `struct Message` (a typed view used by the turn runner: `kind`, `uuid`, `session_id`, `subtype`, `parent_tool_use_id`, result fields, task fields, `backgrounded_to_deliver`).

- [ ] **Step 1: Generate the fixtures** with a script `rust/crates/agentd/tests/fixtures/records/make.py` run in the Python dev image.
- [ ] **Step 2: Write the failing test** `records_match_python_sdk` (each input line → its expected record, compared as JSON values).
- [ ] **Step 3: Run** `just rust-test` — Expected: FAIL.
- [ ] **Step 4: Implement `to_record` and `Message`.**
- [ ] **Step 5: Run** `just rust-test` — Expected: PASS.
- [ ] **Step 6: Commit** `agentd: transcript records in the SDK's format`.

### Task 6: Containers

**Files:**
- Create: `rust/crates/agentd/src/containers.rs`
- Test: unit tests (container spec) and `rust/crates/agentd/tests/containers_docker.rs` (`#[ignore]`, run against real Docker in the live check)

**Interfaces:**
- Consumes: `Config.containers`, `Db`.
- Produces: `struct Containers` with `async fn ensure(&self, conversation: &str) -> Result<()>` (create, start or reuse; serialised per conversation; replaces a container marked `replace` or older than `keep_stopped_days` stopped), `async fn sweep(&self) -> Result<()>` (idle stop, 24-hour stop, 7-day delete, old logs removed), `async fn stats(&self) -> Result<Vec<ContainerStats>>` (for stage 4's Host page), `fn spec(&self, conversation) -> bollard::container::Config<String>` (name `agent-conv-<id>`, labels `agent.conversation`, `agent.kind=session`, image, `Init`, `NetworkMode container:agent-docker`, mounts, limits, `Cmd ["agent-holder","serve","--conversation",<id>]`, user 1001).

- [ ] **Step 1: Write failing tests**: `spec_has_limits_network_mounts_and_labels`, `ensure_is_serialised_per_conversation` (two concurrent `ensure` calls on a fake engine trait → one create), `sweep_deletes_week_old_containers_and_logs` (fake engine, fixed clock), and the ignored Docker tests `starts_reuses_restarts_and_deletes` and `idle_stop_skips_container_with_listener`.
- [ ] **Step 2: Run** `just rust-test` — Expected: FAIL.
- [ ] **Step 3: Implement**, with Docker behind a small `trait Engine` (create, start, stop, remove, inspect, list) whose real implementation uses bollard.
- [ ] **Step 4: Run** `just rust-test` — Expected: PASS (ignored tests excluded).
- [ ] **Step 5: Commit** `agentd: one container per conversation`.

### Task 7: Turn preparation and admission

**Files:**
- Create: `rust/crates/agentd/src/prepare.rs` (port of `Runner._prepare` and `core/bundles.py`), `rust/crates/agentd/src/admission.rs`
- Test: unit tests

**Interfaces:**
- Produces: `fn build_bundle(config) -> Result<PathBuf>` (core plugin, effortless plugin, every pack's plugin, under `/var/agent/bundles/<version>/plugins/...`, version = content hash as today), `fn memory_block(dir, max_lines) -> String`, `fn expand_mcp(servers, env) -> Value`, `fn claude_argv(spec, session_id_or_resume) -> Vec<String>`, `fn claude_env(spec, files) -> BTreeMap<String,String>`, `struct Admission` with `fn room(&self, running: usize) -> bool` (`running < max_concurrent_sessions` and `MemAvailable` from `/proc/meminfo` ≥ `min_free_gib`).

- [ ] **Step 1: Write failing tests**: `memory_block_matches_python` (fixture), `expand_mcp_drops_server_with_unset_variable`, `env_forces_proxy_and_config_dir_last`, `argv_resume_and_fresh`, `bundle_version_stable_for_same_content`, `admission_refuses_when_memory_low`.
- [ ] **Step 2: Run** `just rust-test` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `just rust-test` — Expected: PASS.
- [ ] **Step 5: Commit** `agentd: turn preparation and admission`.

### Task 8: The turn runner

**Files:**
- Create: `rust/crates/agentd/src/turn.rs` (state machine), `src/turn/fold.rs` (state rebuilt from log entries), `src/handoff.rs` (port of `handoff.py`), `src/usage.rs` (port of `usage.py`), `src/proxy.rs` (`whoami`, `usage`, statuses and soft-cap rules from `src/agent/proxy.py`)
- Test: unit tests in each file; `rust/crates/agentd/tests/turn.rs` with `serve` + `fake-claude` and a fake proxy (axum on a random port)

**Interfaces:**
- Consumes: `holder-proto`, `records`, `Db`, `Containers::ensure`, `prepare::{claude_argv, claude_env}`, `Admission`.
- Produces: `struct TurnSpec { work_item, conversation, prompt, origin, cwd, model, system_prompt, idle_s, timeout_s: Option<u64>, env, mcp_servers, plugins: Vec<PathBuf>, settings, tools: Vec<ToolSpec>, resume_session_id: Option<String>, resume_account: Option<String>, attempts: u32 }`; `struct TurnResult` (the fields of today's `/agent/result` body: `outcome`, `text`, `session_id`, `turns`, the four token counts, `cost_usd_est`, `account_code`, `forks`, `error`, `read`); `trait TurnHost { async fn event(&self, work_item, record: Value); async fn tool(&self, work_item, name, args) -> String; }` (stage 3 implements it); `struct TurnRunner` with `async fn run(&self, spec: TurnSpec, host: Arc<dyn TurnHost>) -> TurnResult`, `async fn deliver(&self, work_item, item_id, text, from_person: bool) -> bool`, `async fn stop(&self, work_item)`, `async fn reattach(&self, host) -> Vec<JoinHandle<TurnResult>>`.
- Ported behaviour (claude.py, worker.py): resume only when the proxy pins the session to `resume_account` and the account isn't in trouble, else a fresh session with the handoff note; a failed resume before the first model turn retries fresh with `FRESH_NOTE`; `RESTARTED_NOTE` when `attempts > 0` and resuming; mid-turn messages as `origin + MIDTURN_NOTE + text`, `priority:"now"` plus `origin:{"kind":"human"}` when from a person; stalled after `idle_s` with no output; `timeout` at `timeout_s`; a Claude Code exit without a result resumes once with `RESTARTED_NOTE`, then `error` keeping the session; usage from the CLI's logs, never below the results' own counts, sent as `RunUsage` records when it grows (every 30 s).

- [ ] **Step 1: Write failing tests** in `tests/turn.rs` (fake-claude scenarios):
  - `plain_turn_done` — text joined, tokens, `outcome: "done"`.
  - `result_before_prompt_echo_is_ignored`.
  - `midturn_message_read_and_reported` — delivered, echoed, in `read`; sent with `priority:"now"` when from a person.
  - `background_agent_makes_result_interim`.
  - `moved_shell_holds_turn_until_notification`.
  - `stalled_after_idle_s`, `timeout_at_budget`.
  - `crash_resumes_once_then_errors_keeping_session`.
  - `rejected_account_moves_to_fresh_session_with_handoff`.
  - `soft_cap_moves_only_at_quiet_moment` and `soft_cap_forced_after_30_minutes` (clock injected).
  - `restart_after_result_reports_once`, `reattach_mid_turn_reads_missed_output` (drop the runner mid-turn, start a new one, `reattach`).
  - `tool_call_replayed_after_restart_runs_once`.
  - Unit: `handoff_note_matches_python` (fixture from `handoff_note` on a sample record), `usage_counts_each_reply_once_from_since`.
- [ ] **Step 2: Run** `just rust-test` — Expected: FAIL.
- [ ] **Step 3: Implement**: the runner's state is `fold(log entries)` plus `agentd_turns`; events forwarded past `forwarded_seq` only; tool results stored in `agentd_tool_calls` before they are sent to the holder.
- [ ] **Step 4: Run** `just rust-test` — Expected: PASS.
- [ ] **Step 5: Commit** `agentd: the turn runner`.

### Task 9: Live check on the host

- [ ] **Step 1:** Build `agent-holder` into the agent image (a `COPY --from` of the release binary) and run the ignored Docker tests on the host: `cargo test -p agentd --test containers_docker -- --ignored`. Expected: PASS.
- [ ] **Step 2:** Run one real turn through `TurnRunner` from a small `agentd` debug subcommand (`agentd run-turn --conversation spike --prompt "reply pong"`), then the same with a `docker restart` of the process running it mid-turn. Expected: `pong`; after the restart the turn finishes and reports once.
- [ ] **Step 3: Commit** `agentd: live check of the runtime` with the results appended to the spike results file.
