# Autonomous Engineering Agent v3: One Container, Measured

**Status:** Draft for review, 2026-11-13. This phase runs the agent in one container. The isolation design in
`2026-11-13-general-agent-sandboxes-design.md` (G2–G4, G9, §4–§5) was removed in the one-queue phase.

## 1. Approach

This phase gives the agent a full development environment in one container, scoped to a fork and to staging,
and measures what it delivers. The isolation switch and the v2 code were removed in the one-queue phase: open
mode is the only mode, and the KPIs keep showing what the agent's access costs or gains.

Measuring first gives us the baseline for that comparison. It also shows which access the agent actually needs
before we restrict it. Observability is the principle throughout.

## 2. Decisions

| # | Decision | Detail |
|---|---|---|
| O1 | Goal | Prove the productivity gain, and measure it continuously (§4). |
| O2 | Isolation switch | The `[sandbox] isolation` switch and the v2 code were removed in the one-queue phase. Open mode — which changes only what `core` registers and how runners start — is the only mode. |
| O3 | One agent container | The compose service `agent` is built from the Dockerfile target `agent`. It runs as user `agent` (uid 1001, the `[sandbox] agent_uid` setting) with passwordless sudo. Bash is the login shell and `/bin/sh` (models are biased toward bash). It has open internet, plus git, gh, az, Node 22, Python 3.12 and build tools. Claude Code sessions run with full permissions as that user. |
| O4 | Credentials in the agent | `secrets/agent.env` holds `GH_TOKEN` (for `gh`) and any dev or test keys a project needs, as plain environment variables. The Azure login lives in `data/cli/azure`, which `core` and `agent` share. |
| O5 | Azure | The agent uses the user's own login. An admin runs `az_login` from Slack, which DMs the device code as before. `core` runs the login as the agent's uid, so the agent can use and refresh it. The agent runs `az` itself. Open mode does not register `core`'s `az` tool and confirmations, `ask_production` or the debug queues. |
| O6 | Repos | Each pack points at a fork of its project (`[project] repo = "<owner>/<fork>"`), so PRs open on the fork. The agent's GitHub account needs access to the fork. |
| O7 | `core` | Unchanged as the orchestrator: intake, queue, gate, memory, the Slack/Linear/GitHub tools, the git endpoint, `open_pr` (which keeps every PR linked to its task for the KPIs), transcripts, and secret stripping of Slack posts. `sandboxd` is not used. |
| O8 | Runners | `core` keeps one sandbox token per queue (`code-<project>` for each pack, plus `general`) in `data/runner/runner-tokens.json`, owned by the agent uid. It writes them at startup and renews them in maintenance. The agent container runs one runner per queue in a single process (`python -m agent runners`). A runner restarts when its token changes. |
| O9 | Observability | KPIs (§4), available on demand and as a daily Slack digest. |

## 3. Architecture

```
Slack / Linear ──► core (our code, no LLM): queue, tools, memory, git endpoint, KPIs
                     │ writes runner-tokens.json, runs az_login as the agent uid
                     ▼
                agent container (uid 1001, sudo, internet): runners → Claude Code sessions, full permissions
                     │ pulls work from core:8080 · clones from core's git endpoint · open_pr through core
                     │ gh / az / anything else directly
                     ├──► Claude via the account proxy (host 18091, through the relay on 172.30.0.1:28091)
                     ├──► GitHub fork (core pushes branches and opens draft PRs; the agent may also use gh)
                     └──► Azure (the user's login, in the shared data/cli/azure)
```

Two sessions can run in the container at the same time. Each conversation gets its own repo. Sessions share
the OS user, the ports and the process table.

## 4. KPIs

The window defaults to the last 7 days and covers one project or all of them. For each duration we report
the count, median and p90:

- **Ticket → pickup:** a Linear task is created → its first session starts.
- **Pickup → PR:** the task's first session starts (or the task is created, if a session created it) → its PR opens.
- **PR → merge (merge queue):** the PR opens → it merges.
- **Queue wait:** a turn is queued → a runner claims it. This shows whether capacity is the bottleneck.

Counts: tasks created, PRs opened, merged, closed unmerged, reverted, failed and cancelled, plus the merge rate.
Cost: the total, per task and per merged PR, from the runs' cost estimates (the proxy's accounts).

`core` records a `session_started` event whenever a runner claims a turn. The KPIs are available three ways:
- `just kpis` (`python -m agent kpis --days N --project P`)
- a `kpis` tool in Slack
- a daily digest to `[observability] digest_channel` at `digest_time`, in the config's timezone

Live-deployment time needs a deploy signal, so it waits for later.

## 5. What the agent is told

Open mode uses its own default system prompt. It tells the agent:
- It has a full development environment with its own `gh` and `az`.
- It opens PRs with `open_pr` so they are tracked.
- It works only on the fork and staging, and never touches production.
- Other sessions share the container, so it picks free ports and leaves other processes alone.

Skills say to get Azure signals with `az` directly, or with `ask_production` when the agent has that tool.

## 6. Scope and controls in this phase

- **Scope is the containment.** The agent works on a fork rather than the main repo, and against staging
  rather than production. It opens draft PRs, and a person reviews them before anything merges.
- **Credentials sit with the agent.** The agent container holds the credentials it uses (the fork token, the
  Azure login and test keys), so anything running in it can use them. The isolation phase moves them back into
  `core`.
- **Azure is the user's own account.** The agent is instructed to stay on staging. A staging-only login would
  enforce that.
- **Sessions share the container,** so they can see each other's files and processes.

## 7. Phases

- **Now:** the open agent and KPIs (plan `2026-11-13-open-agent.md`).
- **Later:**
  - merging to staging first, with batch review
  - an always-on QA agent team that dogfoods staging and files tickets
  - a KPI agent team
  - Docker inside the agent, when a project needs it
  - the isolation phase: v2's layers one at a time, with KPIs before and after each

## 8. Jensen as a Linear agent

People assign (delegate) Linear issues to the agent, and follow its work on the issue. It uses Linear's agent
model: an OAuth app installed with `actor=app` and the scopes `read,write,app:assignable,app:mentionable`. Agents
don't take a billable seat. Linear makes the app the issue's *delegate*; a person stays the assignee.

- **Public endpoint.** `core` serves a second, separate listener (container port 8085, published on
  `127.0.0.1:18085`) with exactly two routes: `POST /linear/webhook` and `GET /linear/oauth/callback`. Tailscale
  Funnel exposes that port alone, at `[linear] public_url`. The agent API on 18080 is never exposed.
- **Webhooks.** Every request is checked before anything else:
  - `Linear-Signature` must be the hex HMAC-SHA256 of the raw body with `LINEAR_WEBHOOK_SECRET`.
  - `webhookTimestamp` (ms) must be within 60 s.

  The listener answers 200 at once (Linear allows 5 s) and handles the event in the background.
- **Install.** An admin asks the agent in Slack (`linear_install`, admin tier) and gets the authorize link by DM.
  It carries a one-time `state` that expires in 15 minutes. The callback checks `state` and exchanges the code
  with `LINEAR_CLIENT_SECRET` at `https://api.linear.app/oauth/token`. It then stores the access and refresh tokens
  and the app's own user id. No person handles the token.
- **Tokens.** Access tokens last 24 h, and a refresh rotates the refresh token. `core` refreshes when less than an
  hour is left and keeps the current token in `data/runner/linear.env` (`LINEAR_APP_TOKEN=...`), owned by the agent
  uid, 0600. With the app installed, core's own Linear calls use it, so intake, status sync and comments show as
  the agent. Until then they use `LINEAR_API_KEY`.
- **Agent sessions.**
  - `created`:
    1. Within 10 s, a `thought` activity ("On it").
    2. The issue's team key picks the pack. With no pack for that team, an `error` activity says so.
    3. One task per issue, plus a conversation in `code-<project>`. The issue also gets a Slack thread in the
       pack's channel for visibility.
    4. A turn with the session's `promptContext`.
  - `prompted`: a turn in the same conversation, with `agentActivity.body`.
  - A turn's final text becomes a `response` activity, and a failed turn an `error` activity. `ask` also posts an
    `elicitation`.
  - A session maps to its conversation through settings (`linear_session:<id>`, both ways).
  - Label intake stays as well.
- **MCP in sessions.** `[mcp.<name>]` tables in `agent.toml` (`type = "http"`, `url`, `headers`) go into every
  session. Headers may use `${VAR}`, filled from the session's environment: `agent.env` plus `data/runner/linear.env`,
  which is re-read at each session start. The example config sets `[mcp.linear]` to `https://mcp.linear.app/mcp` with
  `Authorization = "Bearer ${LINEAR_APP_TOKEN}"`, so the agent searches and edits Linear as itself.
- **New secrets (core.env):** `LINEAR_CLIENT_ID`, `LINEAR_CLIENT_SECRET` and `LINEAR_WEBHOOK_SECRET`. `doctor`
  requires them when `[linear] public_url` is set.

## 9. Testing

Unchanged:
- TDD, with `just test` (pytest in the dev image).
- Fakes for Slack, GitHub, the proxy and Claude.
- Bring-up on the laptop with the user.
