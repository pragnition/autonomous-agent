# KPIs From What Core Observes: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** KPIs computed from Linear webhooks plus core's own polling of Linear and GitHub, never from rows the agent writes.

**Architecture:** The webhook handler records each handed-over issue in `linear_issues`. One poller (`LinearIssuePoller`) reads issue state from Linear and another (`AgentPrPoller`) finds the agent's PRs through GitHub search and matches them to issues by key. `compute_kpis` reads only those two tables plus `session_started` events.

**Tech Stack:** Python 3.12, sqlite3, httpx, aiohttp, pytest; dashboard React + TS.

**Spec:** docs/superpowers/specs/2026-11-14-linear-kpis-design.md

## Global Constraints

- Only issues handed over and PRs opened after first start (`settings.kpi_floor`, set once to `utcnow()`).
- Every timestamp comes from Linear or GitHub, never from poll time; `handed_at` = `agentSession.createdAt`.
- No Claude cost in KPIs. No project filter.
- No change to the agent's prompt, skills or tools.
- Timestamps stored as `%Y-%m-%dT%H:%M:%SZ` (`kpis.FMT`); convert Linear/GitHub ISO strings with `_iso()`.

## Review Focus

- A second session on the same issue: one row, `handed_at` stays the earliest. (Task 1 test)
- Key matching: `pra-423-x` branch matches `PRA-423`; `PRA-4230` does not match `PRA-423`. (Task 3 test)
- Reopened issue: close times cleared. (Task 2 test)
- A merged PR isn't fetched again; an open one is. (Task 3 test)
- A PR with no matching issue still counts in totals and the unmatched count. (Task 4 test)

---

### Task 1: Record handed-over issues

**Files:** Modify `src/agent/db.py` (schema), `src/agent/core/linear_sessions.py`; create `src/agent/core/linear_issues.py`; test `tests/test_linear_issues.py`, `tests/test_db.py` (table set).

**Interfaces — produces:**
- Tables:
  - `linear_issues(issue_id TEXT PK, identifier TEXT NOT NULL, title TEXT, url TEXT, handed_at TEXT NOT NULL, state_type TEXT, completed_at TEXT, canceled_at TEXT, checked_at TEXT)`
  - `linear_issue_conversations(issue_id TEXT NOT NULL, conversation_id TEXT NOT NULL, PRIMARY KEY(issue_id, conversation_id))`
  - `agent_prs(repo TEXT NOT NULL, number INTEGER NOT NULL, url TEXT, title TEXT, head TEXT, base TEXT, opened_at TEXT NOT NULL, merged_at TEXT, closed_at TEXT, issue_identifier TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(repo, number))`
- `linear_issues.record_issue(conn, session: dict, conversation_id: str) -> None` — upsert from `agentSession` (`issue.id/identifier/title/url`, `createdAt`); `handed_at = MIN(old, new)`; insert the conversation link (ignore duplicates). No-op if the session has no `issue.id`.
- `linear_issues._iso(value: str | None) -> str | None` — ISO with ms/offset → `FMT`.

- [ ] Test `test_record_issue_once_per_issue`: two sessions for PRA-423 (createdAt 14:39:07.039Z then 14:27:53Z, conversations V-a and V-b) → one row, `handed_at == "2026-11-14T14:27:53Z"`, both conversation links present.
- [ ] Test `test_webhook_records_the_issue` in `tests/test_linear_sessions.py`: a `created` event with an issue → `linear_issues` has the row linked to the queued conversation; an event without an issue → no row.
- [ ] Implement; call `record_issue(self.conn, body["agentSession"], convo_id)` in `on_event` just before enqueueing.
- [ ] Run `pytest tests/test_linear_issues.py tests/test_linear_sessions.py tests/test_db.py -q` → pass. Commit.

### Task 2: Poll Linear for close

**Files:** Modify `src/agent/connectors/linear.py`, `src/agent/core/linear_issues.py`; test `tests/test_linear_issues.py`.

**Interfaces — produces:**
- `LinearClient.issue_state(issue_id: str) -> dict` → `{"state_type": str, "completed_at": str | None, "canceled_at": str | None}` (GraphQL `issue(id) { state { type } completedAt canceledAt }`).
- `class LinearIssuePoller(conn, client, *, clock=lambda: datetime.now(UTC))`, `async poll() -> int` (issues checked). Polls rows with `completed_at IS NULL AND canceled_at IS NULL`, plus rows closed in the last 7 days. Writes state_type, both close times (via `_iso`; NULL when the state isn't completed/canceled), `checked_at`. One failing issue is logged and skipped.

- [ ] Tests with a fake client: open → completed sets `completed_at` from Linear's value; completed → `started` (reopen) clears it; an issue closed 8 days ago isn't polled; one issue raising doesn't stop the others.
- [ ] Implement. Run tests → pass. Commit.

### Task 3: Poll GitHub for the agent's PRs

**Files:** Modify `src/agent/connectors/github_read.py`; create `src/agent/core/agent_prs.py`; test `tests/test_agent_prs.py`.

**Interfaces — produces:**
- `GitHubReader.search_prs(query: str) -> list[dict]` — `/search/issues`, `sort=created`, `order=desc`, `per_page=100`, `q=query`; returns `items`.
- `agent_prs.match_issue(texts: list[str], identifiers: list[str]) -> str | None` — first identifier found case-insensitively as a whole token: regex `(?<![A-Za-z0-9])KEY(?![0-9])`.
- `class AgentPrPoller(conn, reader, *, login: str)`, `async poll() -> int`. Floor = `db.get_setting(conn, "kpi_floor")` (set to `utcnow()` if missing). Query `is:pr author:{login} created:>={floor}`. For each hit, `repo` comes from `repository_url` (last two path parts). Skip hits already stored with `merged_at` or `closed_at` set. Otherwise `reader.pr(repo, n)` → upsert url, title, head ref, base ref, `created_at`, `merged_at`, `closed_at` (all `_iso`), and `issue_identifier = match_issue([title, body, head], identifiers of linear_issues)`. A stored PR keeps a non-NULL `issue_identifier` it already has.

- [ ] Tests: `match_issue(["kunming/pra-423-model"], ["PRA-423"]) == "PRA-423"`; `match_issue(["fixes PRA-4230"], ["PRA-423"]) is None`; the poller stores a merged PR with GitHub's `merged_at` and matched key; a stored merged PR isn't fetched again (fake reader records calls); an unmatched PR is stored with NULL; the search query contains `author:pragnitionlabs created:>=` + the floor.
- [ ] Implement. Run tests → pass. Commit.

### Task 4: KPIs from the new tables

**Files:** Rewrite `compute_kpis`/`Kpis`/`format_kpis` in `src/agent/core/kpis.py`; update `register_kpi_tools` and `main` (drop `project`); `src/agent/core/app.py` digest call unchanged in shape; rewrite `tests/test_kpis.py`.

**Interfaces — produces:**
- `Kpis(since, until, pickup, to_pr, to_merge, to_done_pr, to_done_other, queue_wait: Stat, issues, done, canceled, prs, merged, closed, unmatched: int)` + `merge_rate` property (merged / prs).
- `compute_kpis(conn, *, since, until) -> Kpis`. Pickup start per issue = `MIN(events.ts)` of `session_started` whose `json_extract(data,'$.work_item')` is a work item of a linked conversation. Definitions exactly as in the spec's KPIs section; with-PR = the issue has any `agent_prs` row.
- `format_kpis(k)` lines: Ticket → pickup, Pickup → PR, PR → merge, Ticket → done (with PR), Ticket → done (no PR), Queue wait, `Issues: … · done: … · canceled: …`, `PRs: … · merged: … · closed unmerged: … · not linked to an issue: …`, `Merge rate: …`.

- [ ] Test `test_kpis_from_observed_history`: issue A handed 10:00, first start 10:05, PR opened 11:05, merged 13:05, completed 13:30; issue B handed 10:00, start 10:15, completed 11:00, no PR; one unmatched PR opened 12:00 → pickup `Stat(2, 600, 900)`, to_pr `Stat(1, 3600, 3600)`, to_merge `Stat(1, 7200, 7200)`, to_done_pr `Stat(1, 12600, 12600)`, to_done_other `Stat(1, 3600, 3600)`, `(issues, done, canceled, prs, merged, closed, unmatched) == (2, 2, 0, 2, 1, 0, 1)`, merge_rate 0.5.
- [ ] Keep the `format_duration` and empty-window tests; update `format_kpis` test to the new lines.
- [ ] Implement. Run `pytest tests/test_kpis.py -q` → pass. Commit.

### Task 5: Dashboard

**Files:** `src/agent/dashboard/queries.py` (`kpi_view(conn, days, now, tz)`: daily `{date, issues, prs, merged}`, no `projects`), `src/agent/dashboard/app.py` (drop project query param), `dashboard/src/api.ts` (`KpiView`), `dashboard/src/pages/Kpis.tsx` (no project select; tiles: the 6 time stats; Merge rate; Issues `done · canceled`; PRs `not linked`), `dashboard/src/pages/Overview.tsx` (`Issues` instead of `Tasks`); tests `tests/test_dashboard_queries.py`, `tests/test_dashboard_api.py`.

- [ ] Update the two KPI view tests to seed `linear_issues`/`agent_prs` and assert daily buckets `issues`/`prs`/`merged` and no cost keys.
- [ ] Implement. Run `pytest -q` and `cd dashboard && npx tsc -b && npx vitest run` → pass. Commit.

### Task 6: Wire the pollers and ship

**Files:** `src/agent/core/app.py`; test `tests/test_wiring.py`.

- [ ] `App.__init__`: `self.issue_poller = LinearIssuePoller(conn, linear_client or the sessions' client)` when `linear_sessions` exists; `self.agent_prs = AgentPrPoller(conn, github_reader, login=config.github)` when `github_reader`. `serve`: both on `_every(300, …)`. First start sets `kpi_floor`.
- [ ] Full suite passes. Commit. Hand the user the deploy commands (auto mode can't write to the workstation).
