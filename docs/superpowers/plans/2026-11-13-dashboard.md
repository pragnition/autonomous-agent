# Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A tailnet-only web dashboard, served by core, that shows live sessions, the account pool's usage and the KPIs, with a kill switch.

**Architecture:** A new `agent.dashboard` package in core: read models over `agent.db`, `runs/*.jsonl` and the account proxy, an aiohttp app on port 8086 with an access layer (trusted peer + Tailscale identity), JSON routes, an SSE stream per session, and the built web app as static files. The web app is a Vite + React + TypeScript project in `dashboard/`, built in a Docker stage and copied into the core image.

**Tech Stack:** Python 3.12, aiohttp, SQLite, pytest; Node 22, Vite, React 19, TypeScript, Tailwind CSS v4, shadcn/ui, TanStack Query, React Router, Recharts, Vitest.

**Spec:** docs/superpowers/specs/2026-11-13-dashboard-design.md

## Global Constraints

- The dashboard listener is core port `8086`, published as `127.0.0.1:18086:8086` only; Tailscale Serve exposes it as `--https=9443`. Never on Funnel, never on 443.
- Every `/api` request: peer address in `[dashboard] trusted_peers` (default `["172.30.0.1"]`) and a non-empty `Tailscale-User-Login`, else 403. Every POST also needs `X-Dashboard: 1`, and an `Origin` header, when present, whose host equals the request's `Host`.
- Resume only for a person with `role = "admin"` whose `tailscale` login equals `Tailscale-User-Login` (case-insensitive). Stop and per-session cancel for anyone who gets in.
- Event actor: the matched person's key, else `tailscale:<login>`.
- Agent origins in proxy data: `[dashboard] agent_origins`, default `["agent-core", "agent-sandbox"]`; every other origin counts as people.
- A work item's `payload` is never returned by the API (it carries system prompts and test secrets). Activity text passes through a scrubber that replaces core's secret values with `[secret]`.
- Names stay neutral: the agent is `config.name`; no project names in code or copy.
- Python follows the repo's style (module docstrings, dataclasses, plain functions, 120-column lines); run tests with `just test`.
- Frontend: no runtime CDN; everything bundled. Light and dark theme.
- Only `git add` the files a task touches: the working tree has unrelated uncommitted changes (`deploy/slack-app-manifest.yaml`, `tests/test_config.py`, `claude_proxy/`) that must stay out of these commits.

## Review Focus

1. A transcript line that is truncated (`{"type", "truncated"}`), half-written at the end of the file, or of an unknown shape: the timeline shows what it can and never 500s. Test in Task 2.
2. The proxy down or slow: Accounts says "proxy unreachable" and Overview, Sessions, KPIs still load. Test in Task 4 and Task 6.
3. A session cancelled or finished while its SSE stream is open: the stream sends `end` and closes instead of hanging. Test in Task 6.
4. The agent container calling `core:8086` with forged `Tailscale-User-*` headers: 403. Test in Task 5 (peer check) and checked live in Task 12.
5. Old work items with no transcript file, no conversation, or no `session_started` event: rows still render with nulls. Test in Task 3.

## File Structure

```
src/agent/config.py                 Person.tailscale, DashboardSettings, AgentConfig.dashboard
src/agent/proxy.py                  ProxyClient.raw_usage(), ProxyClient.perf(hours)
src/agent/core/agent_api.py         stamp forwarded lines with "_ts"; `work_started` event for non-turn claims
src/agent/dashboard/__init__.py
src/agent/dashboard/transcript.py   runs/<id>.jsonl -> activity entries; live stats
src/agent/dashboard/queries.py      session rows, events, KPI daily series
src/agent/dashboard/accounts.py     /proxy/usage -> AccountsView; /proxy/perf rows -> history buckets
src/agent/dashboard/access.py       middleware, Viewer, who may resume
src/agent/dashboard/app.py          make_dashboard_app(): routes, SSE, actions, static files
src/agent/core/app.py               build and serve the dashboard
compose.yaml, Dockerfile, justfile, examples/config/agent.toml
dashboard/                          the web app (Vite project)
tests/test_dashboard_*.py
```

## API contract (shared by Tasks 3–11)

`Tokens = {"in": int, "out": int, "cache_read": int, "cache_write": int}`

`SessionRow`:
```
{id, kind, state: "queued"|"running"|"done"|"failed"|"cancelled", sandbox, project, conversation_id, task_id,
 title, source: "slack"|"linear"|"handover"|"chore"|"other", requester, created_at, started_at, ended_at,
 account, turns, tokens: Tokens|null, cost_usd, outcome, error, live: bool,
 links: {slack: str|null, linear: str|null, pr: str|null}}
```
(`state` maps `claimed` to `running`. `live` is true while running, when account and tokens are live estimates.)

`Entry`:
```
{id: "<line>:<n>", ts: str|null, kind: "text"|"tool_call"|"tool_result"|"result"|"rate_limit"|"raw",
 text?, tool?, tool_use_id?, summary?, input?, is_error?, content?, turns?, cost_usd?, usage?: Tokens, subtype?}
```

| Route | Response |
|---|---|
| `GET /api/me` | `{login, name, person, can_resume}` |
| `GET /api/overview` | `{paused, paused_by: {actor, at}\|null, running: [SessionRow + {recent: Entry[≤5]}], queued: [SessionRow], counts: {running, queued, done_today, failed_today}}` |
| `GET /api/sessions?state&project&source&account&q&limit=50&offset=0` | `{rows: SessionRow[], total}` (limit ≤ 200) |
| `GET /api/sessions/{id}` | `SessionRow + {entries: Entry[], offset: int}`; 404 if unknown |
| `GET /api/sessions/{id}/stream?offset=N` | SSE `entries` events `{entries, offset}`; `end` event when the item is terminal and the file is drained; `: ping` every 15 s |
| `POST /api/sessions/{id}/cancel` | `{ok, state}`; 409 if already terminal |
| `GET /api/accounts` | `AccountsView` (Task 4) |
| `GET /api/accounts/history?hours=6\|24\|168\|720` | `History` (Task 4) |
| `GET /api/kpis?days=7&project` | `{kpis: {...Kpis fields, merge_rate}, daily: [{date, tasks, prs, merged, cost_usd}], cost_by_project: [{project, cost_usd, tasks}]}` |
| `GET /api/events?type&limit=100&before` | `{rows: [{id, ts, type, actor, task_id, data}], types: [str]}` |
| `POST /api/stop` | `{ok, cancelled: [id]}` |
| `POST /api/resume` | `{ok}`; 403 unless `can_resume` |

---

### Task 1: Config and proxy client

**Files:**
- Modify: `src/agent/config.py` (`Person`, people parsing ~line 272, `AgentConfig`, the loader's return ~line 356)
- Modify: `src/agent/proxy.py`
- Modify: `examples/config/agent.toml`
- Test: `tests/test_dashboard_config.py`

**Interfaces:**
- Produces: `Person.tailscale: str` (default `""`); `DashboardSettings(port: int = 8086, trusted_peers: tuple[str, ...] = ("172.30.0.1",), agent_origins: tuple[str, ...] = ("agent-core", "agent-sandbox"))`; `AgentConfig.dashboard: DashboardSettings`; `AgentConfig.person_by_tailscale(login: str | None) -> Person | None`; `ProxyClient.raw_usage() -> dict`; `ProxyClient.perf(hours: float) -> list[dict]` (the `rows` list). Both raise `ProxyUnavailable` like `usage()`.

- [ ] **Step 1: Write the failing tests**: `test_person_tailscale_optional` (a person without `tailscale` loads with `""`), `test_person_by_tailscale_case_insensitive` (`"Parthib@X.com"` finds `tailscale = "parthib@x.com"`; `None` and `""` find nobody), `test_dashboard_defaults` (no `[dashboard]` → port 8086, trusted_peers `("172.30.0.1",)`, agent_origins `("agent-core", "agent-sandbox")`), `test_dashboard_overrides` (`[dashboard] port = 9000, trusted_peers = ["10.0.0.1"]`), `test_dashboard_bad_port` (`port = "x"` → `ConfigError`), and for the proxy with `httpx.MockTransport`: `test_raw_usage_returns_json`, `test_perf_returns_rows` (asserts `hours=24` in the query), `test_perf_unreachable_raises`. Reuse the config-writing helpers from `tests/test_config.py` by importing them, not editing that file.
- [ ] **Step 2: Run** `just test tests/test_dashboard_config.py` → FAIL.
- [ ] **Step 3: Implement** with the existing `_str`/problem-list helpers; `[dashboard]` is optional.
- [ ] **Step 4: Add** a commented `tailscale = "..."` line to `[people.parthib]` and a `[dashboard]` block with the defaults to `examples/config/agent.toml`.
- [ ] **Step 5: Run** `just test` → all pass (the existing config tests included).
- [ ] **Step 6: Commit** `feat: dashboard settings, a person's Tailscale login and raw proxy reads`.

### Task 2: Transcript parser

**Files:**
- Create: `src/agent/dashboard/__init__.py`, `src/agent/dashboard/transcript.py`
- Modify: `src/agent/core/agent_api.py` (`events` handler: add `"_ts": db.utcnow()` to the stored record; `pull`: append `work_started` with the same data as `session_started` when `item.kind != "turn"`)
- Test: `tests/test_dashboard_transcript.py`, `tests/test_agent_api.py` (two added tests)

**Interfaces:**
- Produces:
  - `parse_record(record: dict, line: int, scrub: Callable[[str], str]) -> list[dict]` (Entries per the contract)
  - `read_entries(path: Path, offset: int = 0, scrub=...) -> tuple[list[dict], int]`: complete lines from byte `offset`; returns the new offset (a trailing line without `\n` is left for next time)
  - `LiveStats(session_id: str | None, tokens: dict, turns: int, cost_usd: float | None, done: bool)` and `live_stats(path: Path) -> LiveStats`
  - `make_scrubber(secrets: list[str]) -> Callable[[str], str]` (values shorter than 8 characters are ignored)

Mapping (the runner forwards Claude Agent SDK messages as dicts):
- `AssistantMessage.content[]`: `{"text"}` → `text`; `{"thinking"}` → skipped; `{"id","name","input"}` → `tool_call` with `summary` = Bash `command`, else `file_path`/`path`/`pattern`/`url`/`query`/`description`, else the tool name; input kept, string values over 2,000 characters cut.
- `UserMessage.content[]` with `tool_use_id` → `tool_result` with `is_error` and `content` flattened to text, cut to 4,000 characters; a `UserMessage` whose content is a string → `text` (the prompt).
- `ResultMessage` → `result` with `subtype`, `turns` (`num_turns`), `cost_usd` (`total_cost_usd`), `usage` as Tokens, `text` = `result`.
- `RateLimitEvent` → `rate_limit` with `text` = the `rate_limit_info.status` and type.
- `SystemMessage` → nothing. `{"type", "truncated"}` and anything else → `raw` with `text` = the first 500 characters.
- Tokens: `in` = `input_tokens`, `out` = `output_tokens`, `cache_read` = `cache_read_input_tokens`, `cache_write` = `cache_creation_input_tokens`.
- `live_stats`: `session_id` from the `SystemMessage` init's `data.session_id`; tokens summed over distinct `message_id`s using the last usage seen for each (the SDK splits one reply into several messages with the same usage); `ResultMessage` replaces the sums and sets `done`.

- [ ] **Step 1: Write the failing tests** with fixtures copied from the real shapes (an init `SystemMessage`, two `AssistantMessage`s sharing `message_id` with identical usage, a Bash `tool_use`, an `Edit` `tool_use`, a `tool_result` list content with `is_error: true`, a thinking block, a `RateLimitEvent`, a `ResultMessage`, a truncated record, an unknown type): `test_bash_call_summary_is_command`, `test_edit_call_summary_is_path`, `test_thinking_skipped`, `test_tool_result_error_flag_and_cut`, `test_result_entry_tokens`, `test_truncated_and_unknown_become_raw`, `test_read_entries_leaves_partial_last_line` (offset stops before it; appending `\n` later yields it), `test_live_stats_dedupes_message_ids` (two messages with the same id and usage count once), `test_live_stats_result_wins`, `test_scrubber_replaces_secrets`, `test_entry_ts_from_stamp` (`_ts` on the record becomes `ts`; missing → `None`). In `tests/test_agent_api.py`: `test_events_are_stamped` and `test_chore_claim_logs_work_started`.
- [ ] **Step 2: Run** `just test tests/test_dashboard_transcript.py tests/test_agent_api.py` → FAIL.
- [ ] **Step 3: Implement** the parser and the two `agent_api.py` changes.
- [ ] **Step 4: Run** `just test` → all pass (KPIs still only read `session_started`).
- [ ] **Step 5: Commit** `feat(dashboard): read session transcripts into activity entries`.

### Task 3: Read models over agent.db

**Files:**
- Create: `src/agent/dashboard/queries.py`
- Test: `tests/test_dashboard_queries.py`

**Interfaces:**
- Consumes: `agent.core.kpis.compute_kpis`, `Kpis`.
- Produces:
  - `SessionFilter(state: str | None = None, project: str | None = None, source: str | None = None, account: str | None = None, q: str | None = None, limit: int = 50, offset: int = 0)`
  - `list_sessions(conn, f: SessionFilter) -> tuple[list[dict], int]`: SessionRows without links or live fields filled (`links` all `None`, `live` false)
  - `get_session(conn, item_id: str) -> dict | None`
  - `overview_counts(conn, now: datetime, tz: str) -> dict` (`done_today`/`failed_today` by the local date in `tz`)
  - `paused_by(conn) -> dict | None`: the newest `stop_all` event when paused
  - `list_events(conn, type: str | None, limit: int, before: int | None) -> dict`
  - `kpi_view(conn, days: int, project: str | None, now: datetime, tz: str) -> dict`

Derivations, in SQL where they filter:
- `source`: `kind = 'chore'` → `chore`; the conversation has a `channel` → `slack`; the task's `source = 'linear'` or a `settings` row `linear_session:%` whose value is the conversation id → `linear`; a conversation with `is_primary = 0` → `handover`; else `other`.
- `title`: the task title, else the first non-empty line of `json_extract(payload, '$.text')` cut to 120 characters; for a Linear session event, the issue's `identifier — title` from the JSON in that text when present.
- `started_at`: the earliest `session_started` or `work_started` event whose `data.work_item` is the id. `ended_at`: the `runs.ended_at`, else `updated_at` for terminal states.
- `account`, `turns`, `tokens`, `cost_usd`, `outcome`, `error` from `runs` (id = work item id) when present.
- `requester`: the task's requester, else `json_extract(payload, '$.actor')`.
- `q` matches the title (case-insensitive substring).
- `kpi_view.daily`: one row per local date in range, zero-filled: tasks created, PRs opened (`pr_stats.opened_at`), merged, cost (`runs.ended_at`). `cost_by_project`: runs joined to tasks, `null` project as `"(none)"`, sorted by cost.

- [ ] **Step 1: Write the failing tests** on a seeded DB (use the fixtures in `tests/agent_fixtures.py` where they fit): `test_rows_newest_first_with_total`, `test_state_maps_claimed_to_running`, `test_filter_by_state_project_source_account_q`, `test_title_from_task_then_payload_then_linear_issue`, `test_source_slack_linear_handover_chore`, `test_started_at_from_events_and_null_without`, `test_row_without_conversation_or_run` (all nulls, no error), `test_payload_never_in_row` (no key contains `payload`, `system_parts` or `test_secrets` values), `test_paused_by_latest_stop`, `test_events_paging_and_types`, `test_kpi_daily_zero_filled_and_cost_by_project`, `test_overview_counts_local_day`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `just test` → pass.
- [ ] **Step 5: Commit** `feat(dashboard): session, event and KPI read models`.

### Task 4: Accounts view and history

**Files:**
- Create: `src/agent/dashboard/accounts.py`
- Test: `tests/test_dashboard_accounts.py`

**Interfaces:**
- Produces:
  - `accounts_view(raw: dict | None, agent_origins: tuple[str, ...], error: str | None = None) -> dict` → `AccountsView`:
    ```
    {available: bool, error, agent_origins: [..],
     pool: {used_7d, target_7d, delta, state, counts: {ahead, on, behind}, unknown, tokens_60m: Tokens,
            burn: {today: Tokens, week: Tokens, since: epoch|null}, hourly: [[epoch, int|null]], session_burn},
     accounts: [{code, status, rank, verified, active, util_5h, reset_5h, util_7d, reset_7d, pace, target_7d,
                 pace_delta, cap_5h, tokens_60m: Tokens, agent_60m: Tokens, people_60m: Tokens,
                 by_origin: {origin: Tokens}, errors_60m: {"429", "limit", "5xx", "upstream_error"},
                 latency_60m: {requests, ttft_p50, ttft_p90, tok_s}}],
     origins: [{name, is_agent, active, tokens_60m: Tokens, requests_60m}]}
    ```
    Accounts sorted by rank (nulls last), then code. `util_*`/`reset_*` from the `[util, reset_epoch]` pairs; missing → null.
  - `history_view(rows: list[dict], hours: int, agent_origins) -> dict` → `History`:
    ```
    {hours, bucket_s, accounts: [code], origins: [name],
     buckets: [{t, by_account: {code: Tokens}, by_origin: {name: Tokens}, agent: Tokens, people: Tokens,
                requests, errors}]}
    ```
    `bucket_s` = 300 for hours ≤ 24, 3600 for ≤ 168, else 21600; buckets aligned to `bucket_s`, every bucket in range present (zeros), from each row's `t`, `accounts`, `origins`, `all.requests`, and errors = `429 + limit + 5xx + upstream_error`.
  - `HOURS = (6, 24, 168, 720)`

- [ ] **Step 1: Write the failing tests** using a trimmed copy of the real `/proxy/usage` shape (two accounts, one with null windows, three origins) and three perf rows: `test_accounts_sorted_by_rank_nulls_last`, `test_util_and_reset_pairs`, `test_agent_vs_people_split`, `test_unavailable_view` (`raw=None, error="proxy unreachable"` → `available` false, empty lists, no exception), `test_history_bucket_sizes`, `test_history_zero_fills_and_sums_rows_in_bucket`, `test_history_agent_people_totals`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → pass.
- [ ] **Step 5: Commit** `feat(dashboard): account usage and history views`.

### Task 5: Access layer and actions

**Files:**
- Create: `src/agent/dashboard/access.py`, `src/agent/dashboard/app.py` (factory with `/api/me`, `/api/stop`, `/api/resume`, `/api/sessions/{id}/cancel`)
- Test: `tests/test_dashboard_api.py`

**Interfaces:**
- Consumes: `AgentConfig.person_by_tailscale`, `DashboardSettings`, `WorkQueue.cancel_running`, `WorkQueue.cancel`, `WorkQueue.get`, `events.append`, `db.set_setting`.
- Produces:
  - `Viewer(login: str, name: str, person: Person | None)` with `.actor -> str` and `.can_resume -> bool`; `viewer(request) -> Viewer`
  - `access_middleware(trusted_peers: tuple[str, ...]) -> aiohttp middleware`: for `/api/*` checks peer (`request.remote`), identity, and on POST the `X-Dashboard` and `Origin` rules; stores the `Viewer`. Static paths need the peer check only.
  - `make_dashboard_app(*, conn, config, proxy, runs_dir: Path, static_dir: Path | None, permalink: Callable[[str, str], Awaitable[str | None]] | None = None, scrub: Callable[[str], str] = str, clock: Callable[[], datetime] = ...) -> web.Application`; later tasks add routes inside it.
- Effects: stop = `paused=1`, `cancel_running()`, event `stop_all` `{cancelled, via: "dashboard"}`; resume = `paused=0`, event `resume_all` `{via: "dashboard"}`; cancel = `queue.cancel(id)`, event `session_cancelled` `{work_item, via: "dashboard"}`.

- [ ] **Step 1: Write the failing tests** with `aiohttp_client` and `request.remote` controlled by a test middleware or `TestServer` peer: `test_untrusted_peer_403` (forged identity from `172.30.0.5`), `test_missing_identity_403`, `test_me_matches_person`, `test_me_unknown_login_member_view`, `test_post_without_header_403`, `test_post_cross_origin_403`, `test_stop_pauses_cancels_and_logs_actor` (event actor = person key), `test_stop_actor_for_unknown_login` (`tailscale:<login>`), `test_resume_admin_only` (member → 403, admin → ok and `paused=0`), `test_cancel_running_item`, `test_cancel_terminal_409`, `test_cancel_unknown_404`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → pass.
- [ ] **Step 5: Commit** `feat(dashboard): Tailscale access and the kill switch`.

### Task 6: Read routes, SSE, static files and wiring

**Files:**
- Modify: `src/agent/dashboard/app.py`
- Modify: `src/agent/core/app.py` (build the dashboard app in `CoreApp.__init__`; serve it in `serve()` on `config.dashboard.port`; `permalink` via `slack.chat_getPermalink`, cached in a dict; `scrub` from `make_scrubber(self.secret_values)`; `static_dir = Path("/app/dashboard")` when it exists)
- Modify: `compose.yaml` (core: `"127.0.0.1:18086:8086"` with a comment that `tailscale serve` exposes it to the tailnet)
- Test: `tests/test_dashboard_api.py`, `tests/test_wiring.py` (one added assertion that the core app builds a dashboard app)

**Interfaces:**
- Consumes: Tasks 2–5.
- Produces: every route in the API contract. Details:
  - Running rows are filled live: `live_stats(runs_dir / f"{id}.jsonl")` for tokens and turns, `proxy.whoami(session_id)` for the account (a `ProxyUnavailable` leaves it null); `overview` adds `recent` = the last 5 entries.
  - `links.slack` = `permalink(channel, thread_ts)` when the conversation has both; `links.pr` = the task's `pr_url`; `links.linear` = the Linear issue URL from the title derivation's JSON when present.
  - `/api/accounts` and `/api/accounts/history` call the proxy; a `ProxyUnavailable` or timeout gives `accounts_view(None, ..., error=str(e))` / `{error, buckets: []}` with status 200. History responses are cached per `hours` for 60 s.
  - The SSE handler polls the file every second from `offset`, sends `event: entries`, ends with `event: end` once `queue.get(id).status` is terminal and a read returns nothing, and sends `: ping` every 15 s.
  - Static: `GET /` and any non-`/api` path serve `static_dir/index.html` unless a file of that path exists under `static_dir/assets`; without `static_dir`, `/` answers a one-line text page saying the web app isn't built.

- [ ] **Step 1: Write the failing tests**: `test_overview_running_has_recent_and_live_tokens`, `test_sessions_route_filters_and_limit_cap` (`limit=1000` → 200 rows max), `test_session_detail_entries_and_offset`, `test_session_detail_404`, `test_stream_sends_appended_lines_then_end` (append a line while connected, then mark the item done → `entries` then `end`), `test_stream_ends_for_cancelled_item`, `test_accounts_proxy_down_200_unavailable`, `test_history_rejects_bad_hours` (400 for `hours=5`), `test_kpis_route`, `test_events_route`, `test_static_fallback_serves_index`, `test_payload_secrets_never_in_any_response` (seed a payload with `test_secrets: {"K": "s3cr3t-value"}` and a transcript line containing it; no response body contains `s3cr3t-value`).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** routes, wiring and the compose port. **Step 4: Run** `just test` → all pass.
- [ ] **Step 5: Commit** `feat(dashboard): API, live session stream and serving from core`.

### Task 7: Web app scaffold, build and recipes

**Files:**
- Create: `dashboard/` (Vite React TS project: `package.json`, `package-lock.json`, `vite.config.ts`, `tsconfig*.json`, `index.html`, `components.json`, `src/main.tsx`, `src/App.tsx`, `src/index.css`, `src/api.ts`, `src/lib/format.ts`, `src/lib/format.test.ts`, `src/components/ui/*`, `src/components/Layout.tsx`, `src/components/ThemeToggle.tsx`, placeholder pages)
- Modify: `Dockerfile` (stage `dashboard`: `FROM node:22-bookworm-slim`, `npm ci`, `npm run build`; `core` stage: `COPY --from=dashboard /dashboard/dist /app/dashboard`), `.dockerignore` (`dashboard/node_modules`, `dashboard/dist`), `.gitignore` (same), `justfile`

**Interfaces:**
- Produces:
  - `src/api.ts`: TypeScript types for every shape in the API contract (`Tokens`, `SessionRow`, `Entry`, `Overview`, `AccountsView`, `History`, `KpiView`, `EventRow`, `Me`), `api.get<T>(path)` and `api.post<T>(path)` (POST sends `X-Dashboard: 1` and JSON), and query hooks `useMe`, `useOverview`, `useSessions(filter)`, `useSession(id)`, `useAccounts`, `useHistory(hours)`, `useKpis(days, project)`, `useEvents(type)`; polling 5 s (history 60 s), `refetchIntervalInBackground: false`.
  - `src/lib/format.ts`: `fmtTokens(n)` (`1234` → `1.2k`, `2_500_000` → `2.5M`), `fmtUsd(x)` (`null` → `—`), `fmtDuration(seconds)` (same units as `kpis.format_duration`: `45m`, `3h 5m`, `2d 4h`), `fmtAgo(iso, now)`, `fmtPct(x)` (`0.554` → `55%`), `fmtReset(epoch, now)` (`in 3h 10m`), `sumTokens(t)`.
  - `Layout`: left nav with Overview `/`, Sessions `/sessions`, Accounts `/accounts`, KPIs `/kpis`, Activity `/activity`; header with the agent's running/paused state, the viewer's name and a theme toggle (system default, stored in `localStorage` in try/catch).
  - `justfile`: `dashboard-serve` (`tailscale serve --bg --https=9443 http://127.0.0.1:18086`, then print `tailscale serve status`), `dashboard-dev` (`cd dashboard && npm run dev`: Vite proxies `/api` to `http://127.0.0.1:18086` adding `Tailscale-User-Login` from `$DASHBOARD_DEV_LOGIN`; works over `ssh -L 18086:127.0.0.1:18086 <host>`).

- [ ] **Step 1: Scaffold** with `npm create vite@latest dashboard -- --template react-ts`, add Tailwind v4 (`@tailwindcss/vite`), shadcn/ui (`npx shadcn@latest init` then add button, card, badge, table, alert-dialog, select, input, tabs, tooltip, progress, skeleton, separator, scroll-area, dropdown-menu), `@tanstack/react-query`, `react-router-dom`, `recharts`, `vitest`.
- [ ] **Step 2: Write** `format.test.ts` with the examples above → `npx vitest run` FAIL; implement `format.ts` → PASS.
- [ ] **Step 3: Implement** `api.ts`, `Layout`, routing, theme, placeholder pages.
- [ ] **Step 4: Verify** `npm run build` succeeds and `docker build --target core -t agent-core-test .` succeeds with `/app/dashboard/index.html` present (`docker run --rm agent-core-test ls /app/dashboard`).
- [ ] **Step 5: Commit** `feat(dashboard): web app scaffold, build stage and recipes`.

### Task 8: Overview page and the kill switch

**Files:**
- Create: `dashboard/src/pages/Overview.tsx`, `dashboard/src/components/KillSwitch.tsx`, `dashboard/src/components/SessionCard.tsx`, `dashboard/src/components/UsageBars.tsx`, `dashboard/src/components/StatTile.tsx`

**Interfaces:**
- Consumes: `useMe`, `useOverview`, `useAccounts`, `useHistory(24)`, `useKpis(7)`, `api.post`, format helpers.
- Produces: `KillSwitch` (also shown in the header): running → a destructive "Stop everything" button behind an alert dialog that says it pauses new work and cancels N running sessions; paused → "Paused by <actor> <ago>" and "Resume" for `can_resume`, disabled with a tooltip otherwise. `SessionCard({row, recent})` (reused by Sessions). `UsageBars({account})` (5h and 7d bars with reset text, reused by Accounts).

- [ ] **Step 1: Build** the page per spec §5.1: KillSwitch, running and queued `SessionCard`s linking to `/sessions/:id`, pool tile (7d used vs target, state), compact per-account `UsageBars`, a 24 h stacked area of agent vs people tokens from `History`, and 7-day KPI `StatTile`s.
- [ ] **Step 2: Verify** with `npm run build` and in the browser against the dev proxy (Task 7 recipe) or the deployed core: Stop opens the dialog, confirming flips the header to paused, Resume returns it.
- [ ] **Step 3: Commit** `feat(dashboard): overview and kill switch`.

### Task 9: Sessions list and session detail

**Files:**
- Create: `dashboard/src/pages/Sessions.tsx`, `dashboard/src/pages/SessionDetail.tsx`, `dashboard/src/components/ActivityTimeline.tsx`, `dashboard/src/lib/activity.ts`, `dashboard/src/lib/activity.test.ts`

**Interfaces:**
- Consumes: `useSessions`, `useSession`, `api.post`, `SessionRow`, `Entry`.
- Produces: `pairEntries(entries: Entry[]) -> TimelineItem[]` in `lib/activity.ts`, attaching each `tool_result` to its `tool_call` by `tool_use_id` (an orphan result stays its own item); `useSessionStream(id, offset)` that appends `entries` events and stops on `end`.

- [ ] **Step 1: Write** `activity.test.ts`: `pairs_result_with_call`, `keeps_orphan_result`, `keeps_order` → FAIL; implement → PASS.
- [ ] **Step 2: Build** Sessions per spec §5.2: table with state badge, kind, sandbox/project, source, title, requester, started (ago), duration, account, tokens, cost, outcome; filters in the URL query string (state, project, source, account, q); pagination by `offset`.
- [ ] **Step 3: Build** SessionDetail per spec §5.3: header with links and Cancel (confirm) while queued/running; the timeline with tool calls as compact rows (icon by tool, summary, expandable input and result, errors marked), assistant text as messages, the result as a footer card; live entries appended through the stream with auto-scroll unless the user scrolled up.
- [ ] **Step 4: Verify** `npm run build`, `npx vitest run`; in the browser, a running session's timeline grows without reloads.
- [ ] **Step 5: Commit** `feat(dashboard): sessions and live session timeline`.

### Task 10: Accounts page

**Files:**
- Create: `dashboard/src/pages/Accounts.tsx`, `dashboard/src/lib/history.ts`, `dashboard/src/lib/history.test.ts`

**Interfaces:**
- Consumes: `useAccounts`, `useHistory(hours)`, `UsageBars`.
- Produces: `toSeries(history: History, by: "account" | "origin" | "kind") -> {data: Record<string, number>[], keys: string[]}` in `lib/history.ts` (kind = in/out/cache_read/cache_write summed over accounts).

- [ ] **Step 1: Write** `history.test.ts`: `series_by_account_has_every_bucket`, `series_by_kind_sums_accounts`, `series_by_origin_keys` → FAIL; implement → PASS.
- [ ] **Step 2: Build** per spec §5.4: pool header; the accounts table (status and rank, `UsageBars`, pace badge, sessions, tokens last hour by kind with the agent/people split, 429s and errors, TTFT p50/p90, tok/s); clicking an account opens `/sessions?account=<code>`; history range tabs 6 h / 24 h / 7 d / 30 d with stacked charts by account and by origin, a by-kind chart, and requests/errors; "proxy unreachable" state when `available` is false.
- [ ] **Step 3: Verify** `npm run build`, `npx vitest run`; the page renders with live proxy data.
- [ ] **Step 4: Commit** `feat(dashboard): accounts and usage history`.

### Task 11: KPIs & cost, and the activity log

**Files:**
- Create: `dashboard/src/pages/Kpis.tsx`, `dashboard/src/pages/Activity.tsx`

**Interfaces:**
- Consumes: `useKpis(days, project)`, `useEvents(type)`, `StatTile`.

- [ ] **Step 1: Build** KPIs per spec §5.5: range (7/30/90 days) and project selects; tiles for the three times (median and p90, with n), queue wait, merge rate, counts, cost total / per task / per merged PR; daily charts (tasks, PRs, merged as bars; cost as a line); cost by project as a bar list.
- [ ] **Step 2: Build** Activity per spec §5.6: a table of events with time, type badge, actor, task, and the data as compact key/values; a type filter; "Load more" using `before`.
- [ ] **Step 3: Verify** `npm run build`; both pages render against the deployed core.
- [ ] **Step 4: Commit** `feat(dashboard): KPIs, cost and the activity log`.

### Task 12: Bring-up on the workstation (with the user)

- [ ] **Step 1:** Push the branch to the workstation's checkout as the earlier deploys did, add `tailscale = "<login>"` to the admins in `data/config/agent.toml`, then `just up`.
- [ ] **Step 2:** `just dashboard-serve`; confirm `tailscale serve status` shows `:9443` without Funnel and `:443` unchanged.
- [ ] **Step 3:** Open `https://<node>.<tailnet>.ts.net:9443` from the user's own machine: `me` shows the right person, a live session streams, Stop and Resume work and appear in Activity with the user's key.
- [ ] **Step 4:** From inside the agent container: `curl -s -o /dev/null -w '%{http_code}' -H 'Tailscale-User-Login: <admin login>' http://core:8086/api/me` → `403`; and `curl -sk https://<node tailscale IP>:9443/api/me` → not a 200 carrying the admin's identity. If the second check returns the admin, stop and fix before sharing the machine.
- [ ] **Step 5:** Share the machine with teammates from the Tailscale admin console (the user does this).
