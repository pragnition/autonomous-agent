FROM python:3.12-slim AS base
RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client ca-certificates \
 && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
# Install the pinned runtime dependencies straight from pyproject.toml, so versions live in one place
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/req.txt \
 && pip install --no-cache-dir -r /tmp/req.txt

FROM base AS dev
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['optional-dependencies']['dev']))" > /tmp/req-dev.txt \
 && pip install --no-cache-dir -r /tmp/req-dev.txt
ENV PYTHONPATH=/app/src

FROM base AS app
COPY src /app/src
COPY scripts /app/scripts
ENV PYTHONPATH=/app/src

# The dashboard's web app, built once; core serves dist/ from /app/web
FROM node:22-bookworm-slim AS dashboard
WORKDIR /dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY dashboard/ ./
RUN npm run build

# azure-cli: core runs az_login with the admin's device code.
FROM app AS core
COPY --from=dashboard /dashboard/dist /app/web
RUN python -m venv /opt/az \
 && /opt/az/bin/pip install --no-cache-dir azure-cli \
 && ln -s /opt/az/bin/az /usr/local/bin/az
CMD ["python", "-m", "agent", "core"]

FROM app AS supervisor
CMD ["python", "-m", "agent", "supervisor"]

# The agent container: Claude Code sessions with a full development environment.
# Bash everywhere (models write bash), sudo for installing what a task needs, gh and az for the agent's own use.
# To test what it builds end to end: Docker (the CLI; the daemon is the stack's own `docker` service) to run a
# project's app, just and bun for projects that use them, and a browser: Playwright's Chromium, driven with
# playwright-cli or a project's own Playwright tests. Fonts so screenshots show emoji and CJK text.
FROM app AS agent
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=docker:29-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:29-cli /usr/local/libexec/docker/cli-plugins /usr/local/libexec/docker/cli-plugins
COPY --from=oven/bun:1.4.4-slim /usr/local/bin/bun /usr/local/bin/bun
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright PLAYWRIGHT_MCP_BROWSER=chromium
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && ln -s /usr/local/bin/bun /usr/local/bin/bunx \
 && apt-get update \
 && apt-get install -y --no-install-recommends sudo curl jq unzip ripgrep build-essential procps lsof \
      fonts-liberation fonts-noto-color-emoji fonts-noto-cjk \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/* \
 && python -m venv /opt/az && /opt/az/bin/pip install --no-cache-dir azure-cli && ln -s /opt/az/bin/az /usr/local/bin/az \
 && pip install --no-cache-dir rust-just \
 && npm install -g @playwright/cli@0.1.24 \
 && playwright-cli install-browser chromium --with-deps \
 && chmod -R a+rwX /opt/ms-playwright \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1001 --shell /bin/bash agent \
 && echo "agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent && chmod 0440 /etc/sudoers.d/agent \
 && ln -sf /bin/bash /bin/sh \
 && git config --system safe.directory '*' \
 && git config --system credential.https://github.com.helper '!gh auth git-credential' \
 && printf '.agent-progress.md\n.agent-out/\n' > /etc/gitignore-agent \
 && git config --system core.excludesFile /etc/gitignore-agent \
 && mkdir -p /var/agent && chown agent /var/agent \
 && ln -s "$(python -c 'import claude_agent_sdk, os; print(os.path.dirname(claude_agent_sdk.__file__))')/_bundled/claude" \
      /usr/local/bin/claude \
 && ln -s /var/agent/claude /home/agent/.claude \
 && chmod -R a+rX /app   # COPY keeps the host's file modes; the agent user must read the code
# The agent's own Claude home, on the work volume: the skills, plugins and MCP servers it installs stay
ENV SHELL=/bin/bash AZURE_CONFIG_DIR=/data/cli/azure DOCKER_HOST=tcp://127.0.0.1:2375 CLAUDE_CONFIG_DIR=/var/agent/claude
USER agent
CMD ["python", "-m", "agent", "runner"]

FROM alpine:3.20 AS relay
RUN apk add --no-cache socat
