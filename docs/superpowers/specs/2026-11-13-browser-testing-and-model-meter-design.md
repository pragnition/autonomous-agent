# Browser testing, the model-call meter and the first project pack

Status: built 2026-11-13 on the open agent (spec v3). Builds on the dashboard spec for where the caps are set.

## Intent

The agent builds features end to end. A feature isn't done until the agent has run the app, used the feature the way
a user would in a real browser, and shown the screenshots in the request's thread while it works. To make that
possible the agent needs to run a project's whole app, sign in to it, and let the app's own model features work,
without real tenant secrets and without an open-ended model bill.

Goals

- Skills for the kinds of work the agent does most: debugging, building a feature, testing in a browser, and finding
  features worth building.
- Browser testing that is mandatory for new features, with the evidence in the thread.
- The first project (ChilliClaw) runs locally in the agent's environment, signed in, with its model calls on the dev
  Foundry deployment and capped.

Non-goals

- A hosted preview environment per branch. Apps run in the agent's own Docker.
- Production data in tests. Local runs use made-up data; PII sanitisation and knowledge search are off.

## Decisions

| | Decision | Why |
|---|---|---|
| B1 | The agent's own Docker daemon is a `docker:29-dind` service; the agent container shares its network namespace and the work volume is mounted at the same path in both | Apps the agent starts are on its `localhost` (dev servers that check the Host header keep working), compose files' bind mounts of a worktree resolve, and nothing runs on the laptop's own Docker. The API listens on loopback only. |
| B2 | `playwright-cli` drives Chromium, one named session per task; the project's own Playwright tests for anything repeatable | The CLI keeps page data out of the context unless asked for, which saves tokens over a browser MCP server. Its upstream skill is vendored next to ours. |
| B3 | Screenshots live in `.agent-out/screenshots/` and go to the thread with `post_files` while the feature is built | People follow the work where they asked for it. `.agent-out/` is never committed. |
| B4 | `open_pr` refuses a feature's first PR until a screenshot or video was posted in the conversation, or `no_ui` gives the reason nothing on screen changed | "Mandatory" needs a check outside the model. The reason goes in the PR body for the reviewer. Bugs and PR updates aren't held. |
| B5 | `post_files` logs every post as a `files_posted` event and says when a conversation has no Slack thread | The evidence check reads the log; the agent is never told files were posted when they weren't. |
| M1 | Apps call their model provider through core's meter (`http://core:8087/llm/<upstream>`), which adds the key, prices each answer's token usage and refuses calls once the day's or month's cap is spent | Caps that the app can't bypass, without changing the app: it only needs a base URL and a placeholder key. The key stays in core. |
| M2 | Caps start from `[llm_meter]` in agent.toml and are changed at runtime from the dashboard (`set_caps`, admins only); days and months follow the config's timezone | The team adjusts the budget without editing files or restarting. |
| M3 | Prices are per million tokens by model, matched by the longest listed name inside the deployment name, with a deliberately high default | Deployment names carry the model name; an unknown model is over-counted, never free. |
| M4 | Streaming chat completions are asked to report usage in the last chunk; answers without usage are logged as free with a warning | Counting needs the provider's own usage report. |
| M5 | `stop_all` pauses the meter too | The kill switch stops everything the agent started, including its apps' model calls. |
| P1 | ChilliClaw runs in `dev-lite` with an overlay that turns off the integrations needing a real tenant and sends every chat model call through the meter; setup writes a `.env` of local-only values | No real secrets in the agent's environment; the stack boots the same way every time. |
| P2 | The agent signs in through ChilliClaw's dev impersonation switch as a seeded admin | SSO with MFA can't run headless, and the switch is refused in production builds. |
| P3 | One ChilliClaw stack at a time; sessions never stop another session's stack, and always bring their own down | Its ports are fixed. |

## Approaches considered

- **Host Docker socket in the agent container** instead of dind: bind mounts would resolve on the host (the worktree
  isn't there) and published ports would land on the laptop. Rejected.
- **Running the app without Docker** (Postgres and Redis from apt, the app's processes directly): undocumented for the
  first project and loses its document, Office and DSL services. Rejected.
- **A browser MCP server** instead of `playwright-cli`: richer page state, but every step puts the accessibility tree
  in the context. Kept as an option if a project needs it.
- **Azure budgets** for the caps: they alert after the fact and can't stop a call. **LiteLLM** as the meter: budgets
  and keys built in, but another service with its own admin UI. The meter is about 300 lines in core and its caps
  live where the dashboard already reads.

## Left open

- The pack's `slack_channel` for work that starts outside Slack.
- Per-project or per-task caps, if one shared cap proves too coarse.
- A committed end-to-end suite in the first project; until then the agent adds component browser tests and lists the
  steps it ran in the PR.
