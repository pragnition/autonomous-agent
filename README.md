# Autonomous agent

A self-hosted, fully autonomous engineering agent for a team. People message it in Slack, hand it Linear issues or
put work on a schedule, and each conversation runs as a Claude Code session on the agent's own machine: a shell
with sudo, the internet, its own Docker and headless browser, and its own GitHub, Azure and Slack logins. It does
whatever its credentials reach, end to end and without a person in the loop: answers questions, investigates
incidents, builds, tests and ships code, sweeps codebases and files issues, reports on schedules, messages people
and works in any other system it has a key for. It installs its own skills, plugins and MCP servers, so giving it
a credential is all it takes to give it a new ability. A control plane (`agentd`, in Rust) puts every message on
one work queue, runs each conversation in a Docker container of its own and holds the stack's own credentials;
Claude access goes through a Claude account proxy on the host. The whole stack runs with Docker Compose on one
machine.

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
   the sample pack into one of your projects (`repo`, `owner`, `slack_channel`, `linear_team`), or start
   from `examples/pack-templates/`. `data/config/persona.toml` is optional.

   The secrets are `KEY=value` files, mode 0600, and all three must exist:

   ```sh
   # data/secrets/core.env
   SLACK_BOT_TOKEN=xoxb-…
   SLACK_APP_TOKEN=xapp-…
   LINEAR_API_KEY=lin_api_…
   GITHUB_READ_TOKEN=…

   # data/secrets/agent.env: the agent's own keys, as environment variables in every session. It can do whatever
   # these allow, and any key added here is a new system it can work in. GH_TOKEN is how it clones, pushes and
   # opens PRs, so it needs write access to the projects' repos
   GH_TOKEN=…

   # data/secrets/supervisor.env: optional, may be empty
   SLACK_ALERT_WEBHOOK=https://hooks.slack.com/services/…
   ```

   Set `AGENT_NAME` and `AGENT_GIT_EMAIL` (the agent's GitHub noreply address) in the environment or in a `.env`
   file beside `compose.yaml`, for the git identity of its commits.

3. **Start the stack:** `just up` (it also writes `.env` with the Docker socket's group and the data folder's
   path, which the control plane needs to start the conversations' containers).

4. **Check the install:** `just doctor`. No check should read `FAIL`. The agent's Azure login comes from an admin:
   as an admin, DM the agent to run `az_login`, and the device code arrives by DM.

5. **Run one turn end to end:** `just smoke` queues a turn and waits for its conversation's container to answer it
   through the account proxy. It prints `smoke ok`.

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
- [deploy/](deploy/): the Slack app manifest, the account proxy's systemd unit and origins, and the switch from the
  Python control plane ([deploy/cutover.md](deploy/cutover.md)).

`just test` runs the Python tests in a dev image; `just dashboard-test` runs the dashboard's tests and build.
