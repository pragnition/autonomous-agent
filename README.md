# Autonomous agent

A self-hosted engineering agent for a team. Engineers message it in Slack or hand it Linear issues, and each
conversation runs as a Claude Code session with a full development environment: it answers questions,
investigates, fixes bugs and builds features, tests them end to end in its own Docker and a headless browser, and
opens draft pull requests that a person reviews and merges. A core service holds every credential, puts every
message on one work queue and applies the rules in the tools it gives the sessions; Claude access goes through a
Claude account proxy on the host. The whole stack runs with Docker Compose on one machine.

## Quick start

You need Docker with Compose, [just](https://github.com/casey/just) and Tailscale on the host, and the Claude
account proxy running there. The account proxy is a separate program and is not in this repository: it spreads
sessions across the team's Claude accounts and reports their usage. Put it at
`claude_proxy/claude-fallback-proxy.py` in this checkout (the folder is ignored by git), then run
`just proxy-install`, which installs a systemd user service that runs it from there. Add the origins in
`deploy/proxy-origins.example.toml` to `~/.claude/accounts/proxy.toml` and start the service.

1. **Create the Slack app** from `deploy/slack-app-manifest.yaml` (api.slack.com/apps, Create New App, From an app
   manifest), with the name set to your agent's name. Install it in the workspace, keep its bot token (`xoxb-…`)
   and create an app-level token with `connections:write` (`xapp-…`) for Socket Mode.

2. **Write the config and the secrets files.**

   ```sh
   mkdir -p data/config/packs data/secrets/projects
   cp examples/config/agent.toml data/config/
   cp -r examples/config/packs/sample data/config/packs/
   ```

   In `data/config/agent.toml`, set `name`, `github` and `[people]` (Slack user ids, Linear emails, roles). Turn
   the sample pack into one of your projects (`repo`, `owner`, `slack_channel`, `linear_team`, `[build]`), or start
   from `examples/pack-templates/`. `data/config/persona.toml` is optional.

   The secrets are `KEY=value` files, mode 0600, and all three must exist:

   ```sh
   # data/secrets/core.env
   SLACK_BOT_TOKEN=xoxb-…
   SLACK_APP_TOKEN=xapp-…
   LINEAR_API_KEY=lin_api_…
   GITHUB_READ_TOKEN=…
   GITHUB_WRITE_TOKEN=…

   # data/secrets/agent.env: the agent's own keys, as environment variables in its container
   GH_TOKEN=…

   # data/secrets/supervisor.env: optional, may be empty
   SLACK_ALERT_WEBHOOK=https://hooks.slack.com/services/…
   ```

   Set `AGENT_NAME` and `AGENT_GIT_EMAIL` (the agent's GitHub noreply address) in the environment or in a `.env`
   file beside `compose.yaml`, for the git identity of its commits.

3. **Start the stack:** `just up`.

4. **Check the install:** `just doctor`. No check should read `FAIL`. The agent container's Azure login comes from
   an admin: as an admin, DM the agent to run `az_login`, and the device code arrives by DM.

5. **Run one turn end to end:** `just smoke` queues a turn and waits for the agent container to answer it through
   the account proxy. It prints `smoke ok`.

6. **Open the dashboard:** `just dashboard-serve` puts it on the tailnet only, at
   `https://<node>.<tailnet>.ts.net:9443`. Set your Tailscale login as `tailscale` under your `[people]` entry to
   resume the agent and change the meter's caps from there.

Then DM the agent in Slack.

## More

- [docs/design.md](docs/design.md): the components, how a message becomes a session, projects and packs, tools,
  memory, PRs, KPIs, the model-call meter, every config file, operations and the security posture.
- [examples/config/](examples/config/): `agent.toml`, the shipped persona and a minimal pack.
- [examples/pack-templates/chilliclaw/](examples/pack-templates/chilliclaw/): a full pack, with app setup,
  local-run skills and model calls through the meter.
- [deploy/](deploy/): the Slack app manifest and the account proxy's systemd unit and origins.

`just test` runs the Python tests in a dev image; `just dashboard-test` runs the dashboard's tests and build.
