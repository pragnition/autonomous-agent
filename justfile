set shell := ["bash", "-cu"]

dev_image := "autonomous-agent-dev"

# Run the test suite in the Python 3.12 dev image (the host needs only Docker)
test *args:
    docker build -q --target dev -t {{dev_image}} . >/dev/null
    docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD":/app -w /app {{dev_image}} pytest -q {{args}}

# Build and start the stack
up:
    docker compose up -d --build

# Stop and remove the stack (data stays under ./data)
down:
    docker compose down

# Follow logs (optionally one service: just logs core)
logs *service:
    docker compose logs -f {{service}}

# Check config, secrets, the proxy, GitHub and Linear, and the agent container's own gh and az logins
doctor:
    docker compose exec core python -m agent doctor
    docker compose exec agent bash -lc 'gh auth status && az account show -o none && echo "ok    agent: gh and az logged in"'

# KPIs: pickup, PR and merge-queue times, merge rate and cost (e.g. just kpis --days 30 --project sample)
kpis *args:
    docker compose exec core python -m agent kpis {{args}}

# Stop everything from the laptop itself (when Slack isn't an option)
stop:
    docker compose exec core python -m agent stop

# Resume after a stop
resume:
    docker compose exec core python -m agent resume

# Run one real task against the sample pack (needs the full install)
smoke:
    docker compose exec core python /app/scripts/smoke.py

# Put the dashboard on the tailnet only (https://<node>.<tailnet>.ts.net:9443); never Funnel. Run on the laptop.
dashboard-serve:
    tailscale serve --bg --https=9443 http://127.0.0.1:18086
    tailscale serve status

# Work on the dashboard locally against a running core, e.g. after `ssh -L 18086:127.0.0.1:18086 <host>`:
# DASHBOARD_DEV_LOGIN=<your tailscale login> just dashboard-dev
dashboard-dev:
    cd dashboard && npm install && npm run dev

# The dashboard's own tests and production build
dashboard-test:
    cd dashboard && npm ci && npx vitest run && npm run build

# Install the Claude account proxy as a systemd user service (run by a person, once)
proxy-install:
    mkdir -p ~/.config/systemd/user
    sed "s#@REPO@#{{justfile_directory()}}#g" deploy/claude-fallback-proxy.service > ~/.config/systemd/user/claude-fallback-proxy.service
    systemctl --user daemon-reload
    systemctl --user enable claude-fallback-proxy
    loginctl enable-linger "$USER"
    @echo "Installed. Add deploy/proxy-origins.example.toml to ~/.claude/accounts/proxy.toml, then: systemctl --user start claude-fallback-proxy"
