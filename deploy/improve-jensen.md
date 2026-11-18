# improve-jensen host setup

improve-jensen (`.github/workflows/improve-jensen.yml`) runs on a self-hosted GitHub Actions runner on this host and
reaches Claude through the account proxy, so its usage is balanced across the team's accounts like the agent's.

1. **Proxy origin.** Add to `~/.claude/accounts/proxy.toml` under `[origins]`, then restart the proxy
   (`systemctl --user restart claude-fallback-proxy`):

   ```toml
   improve-jensen = 18092
   ```

   To show its usage separately on the dashboard, leave it out of `[dashboard] agent_origins`; to count it as the
   agent, add it.

2. **Runner user.** Run the runner as its own unprivileged user, not in the `docker` group, with no read access to
   this checkout's `data/` or to `~/.claude/accounts`. It runs an agent with a shell on every trigger.

   ```sh
   sudo useradd -m -s /bin/bash gha-improve-jensen
   ```

3. **Runner.** As that user, download the runner (github.com/actions/runner releases, linux-x64) into
   `~/actions-runner`, then register it with a token from
   `gh api -X POST repos/pragnition/autonomous-agent/actions/runners/registration-token --jq .token` (valid 1 hour):

   ```sh
   ./config.sh --url https://github.com/pragnition/autonomous-agent --token <token> \
     --name improve-jensen --labels improve-jensen --unattended
   sudo ./svc.sh install gha-improve-jensen && sudo ./svc.sh start
   ```

4. **Check.** `curl -s http://127.0.0.1:18092/proxy/whoami` as the runner user answers, and the runner shows
   Idle under the repo's Settings → Actions → Runners.
