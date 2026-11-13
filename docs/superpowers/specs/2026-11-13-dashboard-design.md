# Dashboard: what the agent is doing, what it costs, and a kill switch

Status: approved 2026-11-13. Builds on the open agent (spec v3) and the Claude account proxy.

## 1. Intent

The team needs one place to see what the agent is doing right now, how the pooled Claude accounts are being used, and
to stop it. Slack answers one question at a time (`status`, `kpis`, `stop_all`); a dashboard shows everything at
once and keeps the history. Observability is the principle of this phase, so the dashboard is where the KPIs live.

Goals

- See every session live: what it is working on, what it just ran or edited, which account it is on, what it has
  used so far.
- See every account in the pool: 5h and 7d windows, resets, pace, sessions, tokens by kind and by who used them (the
  agent or people), errors, over time.
- KPIs and cost over a chosen range, by project.
- A kill switch anyone with access can press, and a resume only admins can press, both attributed.
- A proper web app, not a status page: navigation, drill-down, charts, filters.

Non-goals

- Changing how the agent works. The dashboard reads what core and the proxy already record, plus the two existing
  actions (stop, resume) and per-session cancel.
- Controlling the proxy (pins, resets, standing accounts down). The proxy keeps its own controls.
- Alerting. Slack already gets the daily digest.

Anti-goals

- Anything reachable from the internet. The Linear listener is on Tailscale Funnel; the dashboard must never be.
- An agent that can resume itself. The agent container must not be able to call the dashboard's actions.

## 2. Approaches

1. **Our own web app (chosen).** React + TypeScript, built to static files and served by core with a JSON API and a
   live stream of session activity. Full control over the live session view, the buttons and drill-down.
   Cost: a Node build stage and a frontend to maintain.
2. Grafana in its own container over core's SQLite and the proxy's JSON. Time-range charts and zoom for free, but the
   live session view and the kill switch become plugin add-ons, and drilling into a session isn't what it is for.
3. A Python dashboard framework (Streamlit and the like). Quick, but it reads as a notebook, not a product.

## 3. Shape

```
 teammate's browser ──tailnet──► tailscale serve :9443 (workstation, tailnet only; adds Tailscale-User-* headers)
                                        │
                                        ▼
                              127.0.0.1:18086 ──► core :8086  dashboard app (aiohttp)
                                                    │  ├─ /            the built web app (static)
                                                    │  ├─ /api/...     JSON read models + stop/resume/cancel
                                                    │  └─ /api/sessions/{id}/stream   SSE tail of runs/<id>.jsonl
                                                    ├─ agent.db (work_items, conversations, tasks, runs, events, pr_stats)
                                                    ├─ data/runs/*.jsonl (each session's Claude Code messages)
                                                    └─ account proxy via the relay (/proxy/usage, /proxy/perf, /proxy/whoami)
```

- core gets a third listener, port 8086, published on the host as `127.0.0.1:18086` only.
- `tailscale serve --bg --https=9443 http://127.0.0.1:18086` puts it on the tailnet as
  `https://<node>.<tailnet>.ts.net:9443`. Funnel stays on 443 for Linear only; the dashboard is a separate port, so
  nothing on it can become public by accident. A `just dashboard-serve` recipe runs this.
- Teammates get access by sharing the machine from the Tailscale admin console: they reach that one device and
  nothing else, and Serve still sends their identity.

## 4. Access

- **Who is asking.** Tailscale Serve adds `Tailscale-User-Login` and `Tailscale-User-Name` to every request,
  including from people a machine is shared with. A request without `Tailscale-User-Login` gets 403.
- **Where it came from.** Identity headers are only trustworthy when nobody can reach the service except through
  Serve. The listener answers only requests whose peer address is in `[dashboard] trusted_peers` (default: the
  Docker gateway `172.30.0.1`, which is how host-published ports arrive in the container). The agent container
  reaches core directly as `core:8086` from its own address, so it gets 403 and can't pose as a person. Bring-up
  verifies this from inside the agent container, and also that the agent container can't get identity headers
  by going through the node's own Tailscale address.
- **What each person may do.**
  - Anyone who gets in: see everything, stop everything, cancel one session.
  - Admins: also resume. A person in `agent.toml` gains an optional `tailscale = "<login>"`; resume needs a person
    with `role = "admin"` whose `tailscale` matches the request's login.
- **Attribution.** Actions append the existing events (`stop_all`, `resume_all`) and a new `session_cancelled`, with
  the actor `<person key>` when the login matches a person, else `tailscale:<login>`.
- **Cross-site requests.** Every POST must carry `X-Dashboard: 1` (a browser form can't set it) and, when present,
  an `Origin` whose host matches the request's `Host`.

## 5. The web app

Pages (left navigation, light and dark theme, everything refreshes itself):

1. **Overview.** Running or paused, with the Stop button (confirm dialog) and Resume for admins. Sessions running and
   queued now, each a card with its title, source, sandbox, elapsed time, account and the last few things it did.
   Pool usage: the pool's 7d used vs target and each account's 5h/7d as compact bars. Tokens per hour, last 24 h,
   the agent vs people. This week's KPIs as stat tiles.
2. **Sessions.** Every work item, newest first: status, kind (turn, chore), sandbox and project, source (Slack,
   Linear, hand-over), title, who asked, started, duration, account, tokens, cost, outcome. Filters for status,
   project, source and a text search over titles. Paged.
3. **Session detail.** The header has everything from the row plus links to the Slack thread, the Linear issue and
   the PR where known, and Cancel while it runs. Below, its activity as a timeline: the assistant's messages, each
   tool call with its input (a Bash command, a file path and diff for edits, a search) and its result folded away,
   and the final result with turns, tokens and cost. While it runs, new entries stream in.
4. **Accounts.** One row per account: status and rank, 5h and 7d used with their reset times, pace (ahead, on,
   behind), sessions now, tokens in the last hour by kind and split by origin (the agent's origins vs the host's),
   429s and errors, TTFT and tok/s. A history section over a chosen range (6 h, 24 h, 7 d, 30 d): tokens per bucket
   stacked by account, and by origin; requests and errors. Clicking an account filters Sessions to it.
5. **KPIs & cost.** The existing KPIs (ticket → pickup, pickup → PR, PR → merge, queue wait as median and p90; tasks,
   PRs, merged, closed, reverted, failed, cancelled; merge rate; cost total, per task, per merged PR) for a chosen
   range and project, plus daily series: tasks, PRs opened, merged, and cost per day; cost by project.
6. **Activity log.** Every event, newest first, filterable by type, with the actor and the task it belongs to.

Stack: Vite, React, TypeScript, Tailwind CSS with shadcn/ui components, TanStack Query (polling every 5 s, paused
while the tab is hidden), React Router, Recharts. The app lives in `dashboard/` at the repo root. A Docker stage builds
it with Node 22 and the core image copies `dist/` in; core serves it with an `index.html` fallback for client routes.
In development, Vite proxies `/api` to a local core.

## 6. The API

All JSON, all under `/api`, all behind §4.

| Route | What |
|---|---|
| `GET /api/me` | login, display name, matched person, `can_resume` |
| `GET /api/overview` | paused; running and queued sessions (summary rows plus last few activity entries each); counts; pool summary |
| `GET /api/sessions?status&project&source&account&q&limit&before` | session rows, newest first, cursor paging |
| `GET /api/sessions/{id}` | one session row plus its full activity |
| `GET /api/sessions/{id}/stream` | SSE: activity entries as they are appended; ends when the item finishes |
| `POST /api/sessions/{id}/cancel` | cancel one queued or running item |
| `GET /api/accounts` | per account and pool, normalised from `/proxy/usage` |
| `GET /api/accounts/history?hours` | buckets from `/proxy/perf`: tokens by kind per account and per origin, requests, errors |
| `GET /api/kpis?days&project` | `compute_kpis` plus daily series and cost by project |
| `GET /api/events?type&limit&before` | the event log |
| `POST /api/stop`, `POST /api/resume` | the kill switch; same effect as `just stop` / `just resume` |

Read models

- **Session row** = a `work_items` row joined to its conversation (sandbox, project, Slack channel and thread, task),
  its task (title, source, requester, PR) and, once finished, its `runs` row (account, turns, tokens, cost, outcome).
  The title is the task's title, else the first line of the message that started it.
- **Activity** comes from `runs/<id>.jsonl`. Each line is a Claude Agent SDK message as the runner forwarded it
  (`SystemMessage`, `AssistantMessage`, `UserMessage`, `RateLimitEvent`, `ResultMessage`, or `{type, truncated}` for
  an oversized one). A parser turns them into entries: `text`, `tool_call` (name, short summary, input),
  `tool_result` (ok or error, truncated content), `result` (turns, cost, usage). Thinking blocks are skipped.
  Unknown shapes become a `raw` entry rather than an error.
- **Live numbers for a running session**: the session id from the `SystemMessage` init, its account from
  `/proxy/whoami`, and tokens summed from the assistant messages' usage so far. The `ResultMessage` replaces the
  estimate when it arrives.
- **Links**: the Slack permalink from `chat.getPermalink`, cached; the PR from the task; the Linear issue from the
  task, or from the Linear session event in the item's payload.
- **Accounts** come from the proxy through the existing relay. `/proxy/usage` is already cached by the proxy for its
  statusline callers, so polling it every 5 s is cheap. History reads `/proxy/perf?hours=N` (5-minute rows, 30 days
  kept) and buckets them server-side (5 min up to 24 h, 1 h up to 7 d, 6 h beyond). When the proxy is unreachable
  the accounts parts say so and the rest of the dashboard still works.

## 7. Changes outside the dashboard

- `config.py`: `Person.tailscale` (optional), `[dashboard]` with `port = 8086`,
  `trusted_peers = ["172.30.0.1"]` and `agent_origins = ["agent-core", "agent-sandbox"]` (the proxy origins that are
  the agent; every other origin counts as people).
- `proxy.py`: `raw_usage()` and `perf(hours)`, beside the existing `usage()`.
- `core/app.py`: build and serve the dashboard app on its port.
- `compose.yaml`: publish `127.0.0.1:18086:8086` for core.
- `Dockerfile`: a `dashboard` build stage; core copies its `dist/`.
- `justfile`: `dashboard-serve` (and `dashboard-dev` for local work).
- `examples/config/agent.toml`: the new fields.

## 8. Testing

- pytest for the API: access rules (no header, untrusted peer, member vs admin, the CSRF header), every read model
  against a seeded database and transcript files, the transcript parser on real message shapes (including truncated
  and unknown lines), SSE delivering appended lines and closing on completion, stop, resume and cancel effects and
  their events, the proxy being down.
- Vitest for the frontend's formatting and transform helpers; `tsc` and the production build in CI via the Docker
  stage.
- Bring-up on the workstation: open it over the tailnet, see a live session stream, press Stop and Resume, and run
  the two agent-container checks in §4.

## 9. Left to the builder

Component layout and visual design within the stack above; chart choices per series (following the dataviz
guidance); exact bucket sizes; how much of a tool result to show folded; page sizes.
