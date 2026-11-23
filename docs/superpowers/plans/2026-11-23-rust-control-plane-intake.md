# Rust Control Plane, Stage 3: Intake Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Everything core does to turn Slack messages, Linear events and schedules into turns, and every tool those turns call, in `agentd`, with stage 2's turn runner underneath.

**Architecture:** New modules in `rust/crates/agentd`, each a port of one Python module, behind `agentd serve`. One SQLite connection (stage 2's `Db`), one Slack client, one Linear client. A dispatcher replaces the runner's pull loop: it claims work items, builds each turn's `TurnSpec`, runs it on the `TurnRunner`, and hands results to the conversation rules. Pure rules are checked against outputs recorded from the Python code; behaviour against fake Slack, Linear and GitHub servers.

**Tech Stack:** stage 2's crates plus `reqwest` with rustls (HTTPS), `tokio-tungstenite` with rustls (Socket Mode), `croner` (cron), `chrono-tz` (time zones), `hmac`/`sha2` (Linear signatures).

**Spec:** `docs/superpowers/specs/2026-11-22-rust-control-plane-design.md`

## Global Constraints

- Same tables, tool names, arguments, tiers (read, act, admin, from the sender, never from arguments), tool replies, Slack texts and reactions as `src/agent/core`; every tool reply and every posted reply is scrubbed of secrets.
- Deliberate changes from Python, from the spec: an error keeps the conversation's session (the next turn resumes or hands off); a message for a conversation with a running turn is delivered at once, not at the next 15-second heartbeat; there is no runner token, runner API, lease or bundle download.
- The Python core and runner are frozen; recorded Python outputs are the target.
- The public repo rule: no keys, host names or people in code, docs, tests or commits.
- Not built here: the dashboard API, LLM meter, KPIs, digest, labeler, task record, PR and issue pollers, admin commands (stage 4).

## Review Focus

- Slack retries an event after agentd restarts: answered once (`slack_events` claim) — `conversations::tests::retried_event_handled_once`.
- The host clock jumps forward after a sleep: a schedule fires once for the missed slots, a park carries on once — `schedules::tests::clock_jump_fires_once`, `parking::tests::due_park_resumes_once`.
- A message arrives while its conversation's turn is ending: it is read by that turn or runs as the next one, never lost — `dispatch::tests::message_during_turn_end_not_lost`.
- A Slack reply longer than one message: split on lines, code fences closed and reopened — `slack::tests::markdown_chunks_match_python`.
- A Linear webhook replayed or forged: refused (signature, one-minute window) — `linear::tests::webhook_refuses_bad_signature_and_stale`.

---

### Task 1: Text rules, secrets, tasks and events

**Files:** Create `src/scrub.rs` (scrub, load_secrets), `src/tasks.rs` (tasks lifecycle, events append), `src/persona.rs` (persona render, token estimate), `tests/fixtures/intake/make.py` and `expected.json` (one fixture for every pure rule in stage 3).

- [ ] Failing tests: `scrub_matches_python`, `load_secrets_matches_python`, `task_transitions_match_python` (legal and illegal moves, events written), `persona_matches_python`.
- [ ] Implement; `just rust-test` green; commit `agentd: text rules, secrets and tasks`.

### Task 2: The work queue and the gate

**Files:** Create `src/queue.rs`.

- Produces: `WorkQueue { enqueue, get, claim, has_claimable, deliver, mark_read, release, complete, cancel, cancel_running, claimed_count, requeue_orphans }`, `Gate::can_start(proxy, admission, running) -> (bool, reason)`. Claimed means agentd runs it; at start, a claimed item with no running or finished turn row is queued again (attempts + 1, failed at 5).
- [ ] Failing tests: `one_claim_per_conversation`, `delivered_then_read_or_released`, `complete_marks_read_before_release`, `hold_scheduled_keeps_schedules_waiting`, `orphan_claim_requeued_at_start`.
- [ ] Implement; green; commit `agentd: the work queue`.

### Task 3: Slack

**Files:** Create `src/slack.rs` (Web API client, markdown posting, file upload and download, Socket Mode).

- Produces: `Slack { post, post_markdown, update, ephemeral, permalink, react, unreact, open_dm, channel_name, user_name, replies, history, api_get, upload_files, download_file }`; `socket_mode(app_token, on_event)` acks each envelope before handling, reconnects on close or `disconnect`.
- [ ] Failing tests (fake Slack on axum, fake WebSocket server): `markdown_chunks_match_python`, `markdown_block_rejected_falls_back_to_text`, `upload_goes_through_external_upload`, `socket_mode_acks_and_reconnects`.
- [ ] Implement; green; commit `agentd: Slack`.

### Task 4: Conversations

**Files:** Create `src/conversations.rs` (port of conversations.py, notifier.py, payloads.py's turn payloads), `src/spawned.rs`.

- Produces: `Conversations { handle_slack, intake_linear, start_scheduled, on_start, read, on_result, resume_after_limit, resume_parked }` with the routing, origin lines, model switch, reactions, retries of long builds, usage-limit waits, spawned agents' reports and scheduled failures as in Python, except that errors keep the session.
- [ ] Failing tests (fake Slack): `dm_starts_conversation_and_reacts`, `thread_reply_goes_to_waiting_conversation`, `reply_in_own_thread_starts_new`, `stranger_gets_ephemeral`, `retried_event_handled_once`, `use_model_switches_or_explains`, `result_posts_reply_and_ticks`, `error_keeps_session`, `limit_cut_waits_and_resumes_once`, `build_retries_three_times`, `spawned_agent_reports_to_parent`, `scheduled_failure_reports_in_channel`, plus the pure rules against the fixture.
- [ ] Implement; green; commit `agentd: conversations`.

### Task 5: Tools

**Files:** Create `src/tools.rs` (router, tiers, manifest, the built-in tools), `src/parking.rs`, `src/projects.rs`, `src/github.rs` (reader and gh_* tools), `src/cli_login.rs` (device-code logins).

- Produces: `Tools::context_for(work_item)`, `Tools::manifest(ctx) -> Vec<ToolSpec>`, `Tools::call(ctx, name, args) -> String`. `post_files` reads files from the conversation's folder on the shared volume and refuses paths outside it; `spawn_agent`'s cwd is made absolute against that folder and must exist there.
- [ ] Failing tests: `tiers_decide_manifest_and_calls`, `post_update_posts_and_records`, `ask_sets_waiting_and_needs_input`, `track_and_finish_task`, `start_task_new_session`, `spawn_agent_limits`, `status_lines`, `cancel_task_cancels_items`, `stop_all_and_resume`, `read_thread_dm_and_thread`, `slack_read_allowlist`, `post_files_only_from_folder`, `park_until_and_pool`, `tell_project_states`, `parse_until_matches_python`, `device_code_parsed`.
- [ ] Implement; green; commit `agentd: tools`.

### Task 6: Linear

**Files:** Create `src/linear.rs` (GraphQL client, label poller and status sync, app install and token refresh writing `runner/linear.env`, agent sessions, the public listener on :8085).

- [ ] Failing tests (fake Linear): `webhook_refuses_bad_signature_and_stale`, `session_created_acks_and_queues`, `prompted_follows_same_conversation`, `oauth_state_single_use_and_exchange`, `token_refreshed_ahead_and_written`, `labelled_issue_becomes_one_task`.
- [ ] Implement; green; commit `agentd: Linear`.

### Task 7: Schedules, usage watch, parks

**Files:** Create `src/schedules.rs` (file parse and checks, firing, `agentd schedules`), `src/usage_watch.rs`; parks' tick in `src/parking.rs`.

- [ ] Failing tests: `schedule_parse_matches_python`, `fires_once_per_slot`, `clock_jump_fires_once`, `paused_and_stopped_use_up_slots`, `errors_reported_once`, `account_states_match_python`, `pool_hold_and_alerts_once`, `limit_waits_resumed_when_ok`, `due_park_resumes_once`.
- [ ] Implement; green; commit `agentd: schedules, usage watch and parks`.

### Task 8: The dispatcher and `agentd serve`

**Files:** Create `src/dispatch.rs` (claim, turn spec from a work item, run, result), `src/serve.rs`; modify `src/turn.rs` (a `read` callback when a delivered message is echoed), `src/main.rs`.

- The turn spec: system prompt (custom or default, projects, active projects, persona) plus the memory block; prompt = origin + text + a note of the Slack files saved into the conversation's folder; cwd from the payload when it exists, else the conversation's folder; idle and schedule budgets from config; plugins from the bundle; MCP servers expanded with agent.env and linear.env; resume from the conversation.
- Events go to `data/runs/<work item>.jsonl` with `_ts`, as core wrote them. `session_started` and run rows as core recorded them. Messages for a running conversation are delivered at once; read ones get ✅ at once; unread ones go back to the queue when the turn ends.
- Loops: Linear intake 60 s, schedules 60 s, usage watch 60 s, parks 60 s, Linear token 600 s, container sweep 60 s; Socket Mode when a Slack app token is set.
- [ ] Failing tests (fake Slack, fake Claude, local holders): `dm_to_reply_end_to_end`, `message_during_turn_delivered_and_ticked`, `message_during_turn_end_not_lost`, `stop_all_interrupts_running`, `restart_reattaches_and_reports_once`.
- [ ] Implement; green; commit `agentd: serve`.

### Task 9: Live check without Slack

- [ ] On the host, `agentd serve` with no Slack app token (the live core keeps Slack), a copy of the database, and a turn queued in a conversation without a thread (as the V-smoke checks do): the turn runs in its container, uses tools, records its run. Results appended to the spike results file; commit `agentd: live check of intake`.
