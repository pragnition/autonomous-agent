set shell := ["bash", "-cu"]

dev_image := "autonomous-agent-dev"
rust_image := "autonomous-agent-rust-dev"

# Run the test suite in the Python 3.12 dev image (the host needs only Docker)
test *args:
    docker build -q --target dev -t {{dev_image}} . >/dev/null
    docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD":/app -w /app {{dev_image}} pytest -q {{args}}

# The Rust workspace's tests, in the rust-dev image; cargo's caches stay in rust/ (git-ignored)
rust-test *args:
    docker build -q --target rust-dev -t {{rust_image}} . >/dev/null
    docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e CARGO_HOME=/repo/rust/.cargo-home -v "$PWD":/repo -w /repo/rust {{rust_image}} cargo test --workspace {{args}}

# Build and start the stack
up:
    -just account-names
    deploy/env.sh
    docker compose up -d --build

# Stop and remove the stack (data stays under ./data)
down:
    docker compose down

# Follow logs (optionally one service: just logs control)
logs *service:
    docker compose logs -f {{service}}

# Check config, secrets, the proxy, every model, Docker and the agent image, and the agent's own gh and az logins
doctor:
    docker compose exec control agentd doctor
    docker compose exec services bash -lc 'gh auth status && az account show -o none && echo "ok    agent: gh and az logged in"'

# KPIs: tasks, time per task, finished and shipped, and tokens (e.g. just kpis --days 30)
kpis *args:
    docker compose exec control agentd kpis {{args}}

# Stop everything from the laptop itself (when Slack isn't an option)
stop:
    docker compose exec control agentd stop

# Resume after a stop
resume:
    docker compose exec control agentd resume

# Check the schedules file (data/schedules/schedules.toml) and show when each schedule runs next
schedules:
    docker compose exec control agentd schedules

# One unattended turn end to end (agentd, a conversation's container, the proxy, Claude), no Slack; prints `smoke ok`
smoke:
    docker compose exec control agentd smoke

# Fetch main, rebuild whatever changed (pausing only for a change to compose.yaml), then check the stack live
deploy *args:
    PYTHONPATH=src python3 scripts/deploy.py {{args}}

# Give the dashboard the Claude account holders' names (from the account proxy's config; run on the laptop)
account-names:
    python3 scripts/account_names.py data/config/account-names.toml

# Put the dashboard on the tailnet only (https://<node>.<tailnet>.ts.net:9443); never Funnel. Run on the laptop.
dashboard-serve:
    tailscale serve --bg --https=9443 http://127.0.0.1:18086
    tailscale serve status

# Work on the dashboard locally against a running agentd, e.g. after `ssh -L 18086:127.0.0.1:18086 <host>`:
# DASHBOARD_DEV_LOGIN=<your tailscale login> just dashboard-dev
dashboard-dev:
    cd dashboard && npm install && npm run dev

# The dashboard's own tests and production build
dashboard-test:
    cd dashboard && npm ci && npx vitest run && npm run build

# Install the Claude account proxy as a systemd user service (run by a person, once). The proxy is a separate
# program: put it at claude_proxy/claude-fallback-proxy.py first.
proxy-install:
    mkdir -p ~/.config/systemd/user
    sed "s#@REPO@#{{justfile_directory()}}#g" deploy/claude-fallback-proxy.service > ~/.config/systemd/user/claude-fallback-proxy.service
    systemctl --user daemon-reload
    systemctl --user enable claude-fallback-proxy
    loginctl enable-linger "$USER"
    @echo "Installed. Add deploy/proxy-origins.example.toml to ~/.claude/accounts/proxy.toml, then: systemctl --user start claude-fallback-proxy"
