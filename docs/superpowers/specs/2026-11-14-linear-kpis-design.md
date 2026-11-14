# KPIs from what core observes

## Why

The KPIs read `tasks` and `pr_stats`. Rows in those tables only appeared when the agent called a core tool
(`track_task`, `open_pr`). Since the agent owns its workflow (it clones, pushes and runs `gh pr create` itself),
nothing writes those rows, so a Linear issue the agent finished left every KPI at zero.

The KPIs must come from facts core sees for itself: the Linear webhook that hands an issue to the agent, Linear's
own state for that issue, and GitHub's own state for the agent's PRs. Nothing depends on the agent recording
anything.

## Goals

- Every Linear issue handed to the agent from now on is tracked: when it was handed over, when work started, and
  when Linear says it was completed or canceled.
- Every PR the agent's GitHub account opens from now on is tracked: when it was opened, when it was actually merged
  (GitHub's `merged_at`), or closed unmerged.
- A PR belongs to an issue when the issue's key (e.g. `PRA-423`) appears in the PR's title, body or branch name. A
  coding issue always ends in a PR to `main` that mentions its key.
- An issue that closes with no PR is non-coding work (a report, an investigation). It counts in ticket→done and
  stays out of every PR number. Nothing classifies issues up front.

## Non-goals

- No backfill: only issues handed over and PRs opened after this ships.
- No change to the agent's prompt, skills or tools.
- No Claude cost in the KPIs (Foundry spend has its own page).
- No per-project filter on KPIs: an issue only has a repo once it has a PR, so the filter is dropped.

## Design

### Recording an issue (webhook)

`LinearAgentSessions.on_event` already gets every `AgentSessionEvent`. On any event whose session carries an issue,
core upserts a `linear_issues` row:

| column | source |
|---|---|
| `issue_id` (PK) | `agentSession.issue.id` |
| `identifier`, `title`, `url` | `agentSession.issue` |
| `conversation_id` | the conversation the event is queued to (the first one wins) |
| `handed_at` | `agentSession.createdAt` of the first session (the earliest wins) |
| `state_type`, `completed_at`, `canceled_at` | Linear, by polling |
| `checked_at` | when core last polled it |

Later sessions on the same issue (PRA-423 had three) update the same row, never add one.

### Pickup

The first `session_started` event whose work item belongs to a conversation of that issue. Core already writes
that event. Every follow-up session's conversation is linked too (a `linear_issue_conversations` table), so pickup
is the first start across all of them.

### Closing (poll Linear)

Every 5 minutes core asks Linear for each tracked issue that isn't closed, plus any closed in the last 7 days (to
catch reopens): `state { type }`, `completedAt`, `canceledAt`. Times come from Linear, not from the poll. A reopened
issue gets its close times cleared.

### PRs (poll GitHub)

Every 5 minutes core runs a GitHub search `is:pr author:<agent login> created:>=<floor>`. The floor is when this
shipped (stored in settings on first start). For each hit that isn't merged or closed yet in core's table, core reads
`/repos/{repo}/pulls/{n}` and upserts `agent_prs`: repo, number, url, title, head branch, base branch, `opened_at`,
`merged_at`, `closed_at`, `issue_identifier`.

The issue is matched by searching title, body and head branch for the identifiers of tracked issues
(case-insensitive, whole token, so `pra-423-model-usage` matches `PRA-423` and `PRA-42` doesn't). A PR that matches
no tracked issue is still stored and counted, with `issue_identifier` NULL.

### KPIs

Over a window `[since, until)`:

- **Ticket → pickup**: `handed_at` → first session start, for issues handed over in the window.
- **Pickup → PR**: first session start → the issue's first PR `opened_at`, for PRs opened in the window.
- **PR → merge**: `opened_at` → `merged_at`, for PRs merged in the window.
- **Ticket → done, with PR / without PR**: `handed_at` → `completed_at`, for issues completed in the window.
- **Queue wait**: unchanged.
- **Counts**: issues handed over, completed, canceled; PRs opened, merged, closed unmerged, and unmatched; merge rate
  = merged / opened.

The CLI (`agent kpis`), the agent's `kpis` tool, the daily digest and the dashboard (`/api/kpis`, Overview card,
KPIs page) all switch to these. The dashboard's daily chart becomes issues / PRs / merged per day.

## Approaches considered

- **Match PRs by the URL in the turn transcript.** Needs no convention, but every coding issue's PR mentions its key
  anyway, so the transcript adds nothing but parsing.
- **Linear's GitHub integration (issue attachments).** Not linking PRs in this workspace; would add a dependency on
  someone else's setup.
- **Chosen: issue key in the PR, everything polled by core.**

## Builder's calls

- `handed_at` (delegation) is the ticket start, not the issue's Linear creation time: issues often sit in a backlog
  for days before anyone hands them over.
- The old `tasks`/`pr_stats` tables and `PrStatsPoller` stay as they are (unused by KPIs).
