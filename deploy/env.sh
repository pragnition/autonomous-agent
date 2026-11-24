#!/usr/bin/env bash
# .env for compose: the Docker socket's group (agent-control joins it to start the conversations' containers) and the
# data folder's path on the host (the conversations' bind mounts are resolved there). `just up` runs it.
set -euo pipefail
cd "$(dirname "$0")/.."

DATA="${AGENT_DATA:-./data}"

# One key in .env, keeping whatever else is there
set_env() {
  local key=$1 value=$2
  touch .env
  grep -v "^${key}=" .env > .env.tmp || true
  printf '%s=%s\n' "$key" "$value" >> .env.tmp
  mv .env.tmp .env
}

set_env DOCKER_GID "$(stat -c %g /var/run/docker.sock)"
set_env AGENTD_DATA_HOST "$(realpath "$DATA")"
