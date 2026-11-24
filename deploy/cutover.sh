#!/usr/bin/env bash
# The switch from the Python control plane (agent-core and agent-runner) to agentd (agent-control and
# agent-services), and back. Run from the live checkout, as the person who runs the stack (sudo is needed once, for
# the data folders' owner):
#
#   deploy/cutover.sh env        .env for compose (the Docker socket's group, the data folder's path); `just up` runs it
#   deploy/cutover.sh check      env, the images, and what the switch will change; changes nothing live
#   deploy/cutover.sh switch     pause, let running turns finish (CUTOVER_IDLE_S, default 600 s) or stop them, back up
#                                the database, hand the data folders to the agent user, start agent-control and
#                                agent-services in place of agent-core and agent-runner, resume, doctor, smoke
#   deploy/cutover.sh rollback   stop agent-control and the conversations' containers, start the Python services again
set -euo pipefail
cd "$(dirname "$0")/.."

DATA="${AGENT_DATA:-./data}"
AGENT_UID=1001
IDLE_S="${CUTOVER_IDLE_S:-600}"
POLL_S="${CUTOVER_POLL_S:-15}"

say() { printf '%s\n' "$*"; }
fail() { printf 'cutover: %s\n' "$*" >&2; exit 1; }

# One key in .env, keeping whatever else is there
set_env() {
  local key=$1 value=$2
  touch .env
  grep -v "^${key}=" .env > .env.tmp || true
  printf '%s=%s\n' "$key" "$value" >> .env.tmp
  mv .env.tmp .env
}

write_env() {
  set_env DOCKER_GID "$(stat -c %g /var/run/docker.sock)"
  set_env AGENTD_DATA_HOST "$(realpath "$DATA")"
}

check() {
  [ -f "$DATA/secrets/core.env" ] || fail "$DATA/secrets/core.env is missing"
  [ -f "$DATA/secrets/agent.env" ] || fail "$DATA/secrets/agent.env is missing"
  write_env
  docker compose config -q || fail "docker compose config failed"
  if docker run --rm --entrypoint sh autonomous-agent-agent -c 'command -v agentd agent-holder >/dev/null && test -f /app/web/index.html'; then
    say "ok    image: autonomous-agent-agent has agentd, agent-holder and the web app"
  else
    fail "the agent image has no agentd yet: docker compose build control services supervisor supervisor-standby"
  fi
  for dir in db runs tasks runner cli; do
    [ "$(stat -c %u "$DATA/$dir" 2>/dev/null || echo none)" = "$AGENT_UID" ] \
      || say "todo  $DATA/$dir: switch hands it to uid $AGENT_UID"
  done
  say "ok    check"
}

core_python() { docker exec agent-core python -c "$1"; }

claimed() {
  core_python "from agent import db; c = db.connect('/data/db/agent.db'); \
print(c.execute(\"SELECT COUNT(*) FROM work_items WHERE status = 'claimed'\").fetchone()[0])"
}

switch() {
  check
  say "pausing: nothing new starts"
  core_python "from agent import db; c = db.connect('/data/db/agent.db'); db.set_setting(c, 'paused', '1'); c.commit()"
  local waited=0
  while [ "$(claimed)" != "0" ] && [ "$waited" -lt "$IDLE_S" ]; do
    sleep "$POLL_S"
    waited=$((waited + POLL_S))
  done
  say "stopping what still runs (its conversation keeps its session)"
  docker exec agent-core python -m agent stop
  local backup="agent-before-control-$(date -u +%Y%m%dT%H%M%SZ).db"
  core_python "import sqlite3; s = sqlite3.connect('/data/db/agent.db'); d = sqlite3.connect('/data/db/$backup'); \
s.backup(d); d.close()"
  say "backed up the database to $DATA/db/$backup"
  # the supervisors would restart what is about to be stopped
  docker stop agent-supervisor agent-supervisor-standby
  docker stop agent-core agent-runner
  # agentd runs as the agent user, where core ran as root
  sudo chown -R "$AGENT_UID:$AGENT_UID" "$DATA/db" "$DATA/runs" "$DATA/tasks" "$DATA/runner" "$DATA/cli"
  # and reads the secrets core read as root: its own, the sessions' keys and the projects' app env files
  [ ! -d "$DATA/secrets/projects" ] || sudo setfacl -m "u:$AGENT_UID:rx" "$DATA/secrets/projects" 2>/dev/null \
    || sudo chmod o+x "$DATA/secrets/projects"
  for file in "$DATA/secrets/core.env" "$DATA/secrets/agent.env" "$DATA"/secrets/projects/*.env; do
    [ -f "$file" ] || continue
    sudo setfacl -m "u:$AGENT_UID:r" "$file" 2>/dev/null \
      || { sudo chgrp "$AGENT_UID" "$file" && sudo chmod 0640 "$file"; }
  done
  set_env SUPERVISE_CONTROL "http://control:8080/health"
  docker compose up -d control services supervisor supervisor-standby
  local tries=0
  until docker exec agent-control curl -sf http://127.0.0.1:8080/health >/dev/null 2>&1; do
    tries=$((tries + 1))
    [ "$tries" -lt 30 ] || fail "agent-control isn't healthy: docker logs agent-control; then deploy/cutover.sh rollback"
    sleep 2
  done
  docker exec agent-control agentd resume
  docker exec agent-control agentd doctor || say "doctor reported problems above"
  docker exec agent-control agentd smoke || fail "smoke failed: docker logs agent-control; deploy/cutover.sh rollback"
  say "switched. Check by hand: a DM, a channel mention, a thread reply, a Linear issue, a schedule, the dashboard."
}

rollback() {
  docker stop agent-supervisor agent-supervisor-standby || true
  docker exec agent-control agentd stop || true
  local ids
  ids="$(docker ps -q --filter label=agent.conversation)"
  [ -z "$ids" ] || docker stop $ids
  docker compose stop control services
  set_env SUPERVISE_CONTROL ""
  docker compose --profile python up -d core agent
  docker compose up -d supervisor supervisor-standby
  local tries=0
  until docker exec agent-core python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=5)" \
        >/dev/null 2>&1; do
    tries=$((tries + 1))
    [ "$tries" -lt 30 ] || fail "agent-core isn't healthy: docker logs agent-core"
    sleep 2
  done
  docker exec agent-core python -m agent resume
  say "rolled back to agent-core and agent-runner"
}

case "${1:-}" in
  env) write_env ;;
  check) check ;;
  switch) switch ;;
  rollback) rollback ;;
  *) fail "usage: deploy/cutover.sh env|check|switch|rollback" ;;
esac
