# Rust Control Plane, Stage 4: Dashboard, Meter, KPIs and Admin Commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Everything core does besides intake in `agentd`: the dashboard API and web app on :8086, the LLM meter on :8087 with Azure-billed prices, the task record and its Haiku labels, the KPIs, digest and `kpis` tool, the PR and Linear issue pollers, the Host page, and the admin commands.

**Architecture:** New modules in `rust/crates/agentd`, each a port of one Python module, on stage 3's `Shared`. Read models are pure functions over the database, checked against outputs recorded from the Python code on a seeded database. HTTP on axum, like the Linear listener. The Host page is the one new design: the runner's process sampler is gone, so numbers come from `/proc` (host totals) and Docker (one consumer per container).

**Tech Stack:** stage 3's crates; `bollard` stats for the Host page; no new dependencies unless a task names one.

**Spec:** `docs/superpowers/specs/2026-11-22-rust-control-plane-design.md`

## Global Constraints

- Same `/api/*` routes, query parameters, JSON shapes and status codes as `src/agent/dashboard`; same access rules (trusted peers, `Tailscale-User-Login`, `X-Dashboard: 1` and same-origin on POSTs); same SSE events (`entries`, `end`, `: ping` every 15 s).
- Same meter URLs (`/llm`, `/llm/<upstream>/<path>`), error codes, pricing, caps and `llm_usage` rows; the upstream keys never leave agentd.
- Same tables and columns (`request_labels`, `task_records`, `agent_prs`, `pr_stats`, `linear_issues`, `llm_usage`, `settings` keys); `LABEL_VERSION = 2`, so Python's labels stand.
- Same texts for the `kpis` tool, `agentd kpis` and the digest.
- Every transcript entry shown is scrubbed of secrets.
- The public repo rule: no keys, host names or people in code, docs, tests or commits.

## Review Focus

- A dashboard viewer from the agent's own address, or a cross-site POST: 403 — `dashboard::tests::access_rules`.
- A transcript line half-written when the stream reads it: left for the next read, never parsed twice or lost — `transcript::tests::partial_line_waits`.
- An app hangs up mid-stream through the meter: the usage seen so far is still recorded — `meter::tests::stream_hangup_still_recorded`.
- Haiku down or answering junk: requests wait, then become chat after 3 tries; a relabel that keeps failing keeps its label — `labeler::tests::unreadable_answers`.
- The digest at a restart after its time: posted once that day — `kpis::tests::digest_once_per_day`.

---

### Task 1: KPIs, the `kpis` tool and the digest

**Files:** Create `src/kpis.rs`; `tests/fixtures/dashboard/make.py` (seeds a database through `agent.db`, writes `seed.sql` and `expected.json`); modify `src/tools.rs` (the `kpis` tool, read tier), `src/tools.json`.

- Produces: `tokens_by_project`, `app_tokens_by_upstream`, `usage_windows(conn, now, tz)`, `compute_kpis(conn, since, until) -> Kpis`, `format_kpis(&Kpis, name)`, `format_usage_windows`, `kpis_text(conn, days, now, name, tz)`, `post_digest(s, now) -> bool`.
- [ ] Failing tests: `kpis_match_python` (compute and format for 1, 7 and 30 days), `usage_windows_match_python`, `kpis_tool_checks_days`, `digest_once_per_day`.
- [ ] Implement; `just rust-test` green; commit `agentd: KPIs and digest`.

### Task 2: The task record and its labels

**Files:** Create `src/task_record.rs` (port of task_record.py), `src/labeler.rs` (port of task_labeler.py); modify `src/dispatch.rs` (poke the labeler after a result).

- Produces: `pending`, `origin_of`, `asked_text`, `project_hints`, `context_for`, `apply_label`, `rebuild_task`, `work_seconds`, `export_months`; `render`, `parse_label`, `Labeler { new(s, complete), poke(), sweep() -> usize }`; `haiku(proxy_url) -> Complete` (POST `/v1/messages`, model `claude-haiku-4-5`, max_tokens 400, placeholder token, a fresh session id per sweep).
- [ ] Failing tests: `task_record_matches_python` (pending, origin, context, render, apply and rebuild, relabel moving a request between tasks, month files), `parse_label_matches_python`, `sweep_labels_in_order_per_requester`, `waits_without_capacity`, `unreadable_answers`.
- [ ] Implement; green; commit `agentd: task record and labels`.

### Task 3: PR and Linear issue pollers

**Files:** Create `src/pollers.rs` (ports of pr_stats.py, agent_prs.py and linear_issues.py's poller); modify `src/github.rs` (reader calls), `src/linear.rs` (`issue_state`, `team_state_names`).

- [ ] Failing tests (fake GitHub and Linear): `pr_stats_records_and_moves_task`, `revert_found_within_30_days`, `agent_prs_found_and_tied_to_issue`, `match_issue_matches_python`, `issue_states_polled_until_a_week_after_close`.
- [ ] Implement; green; commit `agentd: PR and Linear issue pollers`.

### Task 4: The LLM meter and Azure rates

**Files:** Create `src/meter.rs` (port of llm_meter.py), `src/azure_rates.rs` (port of azure_rates.py; `az` run directly, agentd already runs as the agent user).

- Produces: `meter_app(s, rates) -> Router`, `meter_view`, `set_caps`, `AzureRates { load, price, view, refresh }`.
- [ ] Failing tests (fake upstream): `price_and_cost_match_python`, `usage_of_matches_python`, `forwards_with_key_and_records`, `streaming_usage_recorded`, `stream_hangup_still_recorded`, `caps_refuse_and_event_once_per_day`, `paused_refuses`, `parse_rates_matches_python`, `refresh_reprices_month`.
- [ ] Implement; green; commit `agentd: LLM meter and Azure rates`.

### Task 5: Dashboard read models

**Files:** Create `src/dashboard/queries.rs`, `src/dashboard/accounts.rs`, `src/dashboard/transcript.rs`, `src/dashboard/mod.rs` (module only); extend the fixture.

- Produces: `SessionFilter`, `list_sessions`, `list_conversations`, `get_conversation`, `conversation_row`, `get_session`, `overview_counts`, `paused_by`, `list_events`, `kpi_view`; `accounts_view`, `history_view`, `bucket_seconds`; `parse_record`, `read_from`, `lines_before`, `TranscriptCache::follow`; `AccountNames`.
- [ ] Failing tests: `queries_match_python` (every filter, paging, events sources, KPI view), `accounts_match_python`, `transcript_matches_python`, `partial_line_waits`, `cache_reads_only_new_bytes`.
- [ ] Implement; green; commit `agentd: dashboard read models`.

### Task 6: The Host page

**Files:** Create `src/host.rs` (sampler, `HostStore`, `host_view`, `consumers_view`).

- The sampler, every 5 s: CPU, memory, load and disk I/O from `/proc` (the host's own); one consumer per container agentd runs (`session` for a conversation's container, linked to its running work item; `service` for the services container; `container` for the network container that runs Jensen's apps; `runner` for agent-control) from Docker stats; network from the network container's stats; ports from the holders' listeners; disk space every 10 minutes (the `agentwork` volume and the data directory, the largest conversation folders).
- [ ] Failing tests: `snapshot_from_proc_and_docker` (fixture `/proc` files and stats), `store_keeps_an_hour_and_splits_by_kind`, `views_match_python` (Python's host_view and consumers_view on the same snapshots).
- [ ] Implement; green; commit `agentd: host page`.

### Task 7: The dashboard API

**Files:** Modify `src/dashboard/mod.rs` (axum router, access middleware, every route, SSE stream, static files with the `index.html` fallback), `src/serve.rs`.

- [ ] Failing tests (`tests/dashboard.rs`, fake proxy and Slack): `access_rules`, `overview_and_conversations`, `session_entries_and_stream_end`, `cancel_stop_resume`, `accounts_and_history`, `llm_caps_admin_only`, `static_and_fallback`.
- [ ] Implement; green; commit `agentd: dashboard API`.

### Task 8: Admin commands and serve wiring

**Files:** Modify `src/main.rs` (`doctor`, `kpis`, `stop`, `resume`, `smoke`), `src/serve.rs` (dashboard :8086, meter :8087, loops: labeler 300 s, digest 300 s, pollers 300 s, Azure 30 min, Linear issues 300 s, host sampler 5 s).

- `smoke`: one unattended turn in a fresh `V-smoke-*` conversation through `agentd ask`, prints `smoke ok`.
- [ ] Failing tests: `doctor_reports_each_check`, `stop_and_resume_offline`.
- [ ] Implement; green; commit `agentd: admin commands`.

### Task 9: Live check

- [ ] On the host, on a copy of the live database: every `/api/*` read from agentd and from the Python dashboard compared (same JSON apart from live numbers), the web app served, a meter call through a stand-in upstream, `agentd doctor`, `agentd kpis`. Results appended to the spike results file; commit `agentd: live check of dashboard and meter`.
