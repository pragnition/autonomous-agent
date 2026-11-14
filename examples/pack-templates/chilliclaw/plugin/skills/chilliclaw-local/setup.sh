#!/usr/bin/env bash
# Set up a ChilliClaw clone to run locally. Run it from the clone's root; it's safe to run again.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
test -f .env || python3 "$here/write-env.py"
bun install --frozen-lockfile
