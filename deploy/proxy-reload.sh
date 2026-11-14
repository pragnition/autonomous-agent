#!/bin/sh
# Reload the Claude account proxy without dropping replies: write the next command, then SIGHUP the running one.
set -eu
repo=$(cd "$(dirname "$0")/.." && pwd)
printf '["/usr/bin/python3", "%s/claude_proxy/claude-fallback-proxy.py", "--config", "%s/.claude/accounts/proxy.toml"]' \
  "$repo" "$HOME" > "$XDG_RUNTIME_DIR/claude-fallback-proxy.next"
kill -HUP "$1"
