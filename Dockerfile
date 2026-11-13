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

# core runs `az` for debug sessions (spec v2 §7); its login lives in /data/cli, never in a sandbox
FROM app AS core
RUN python -m venv /opt/az \
 && /opt/az/bin/pip install --no-cache-dir azure-cli \
 && ln -s /opt/az/bin/az /usr/local/bin/az
CMD ["python", "-m", "agent", "core"]

FROM app AS supervisor
CMD ["python", "-m", "agent", "supervisor"]

# The open agent container (spec v3 O3): Claude Code sessions with a full development environment.
# Bash everywhere (models write bash), sudo for installing what a task needs, gh and az for the agent's own use.
FROM app AS agent
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && apt-get update \
 && apt-get install -y --no-install-recommends sudo curl jq unzip ripgrep build-essential \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/* \
 && python -m venv /opt/az && /opt/az/bin/pip install --no-cache-dir azure-cli && ln -s /opt/az/bin/az /usr/local/bin/az \
 && useradd --create-home --uid 1001 --shell /bin/bash agent \
 && echo "agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent && chmod 0440 /etc/sudoers.d/agent \
 && ln -sf /bin/bash /bin/sh \
 && git config --system safe.directory '*' \
 && mkdir -p /var/agent && chown agent /var/agent \
 && chmod -R a+rX /app   # COPY keeps the host's file modes; the agent user must read the code
ENV SHELL=/bin/bash AZURE_CONFIG_DIR=/data/cli/azure
USER agent
CMD ["python", "-m", "agent", "runners"]

FROM alpine:3.20 AS relay
RUN apk add --no-cache socat
