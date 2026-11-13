---
name: chilliclaw-debugging
description: Use when investigating or fixing a ChilliClaw bug, for where its signals live and how to run it locally.
---

# Debugging ChilliClaw

Fill in the values marked CONFIRM before installing this pack.

**Where the signals are**
- Logs and traces: query them with `az` when you have it (the azure-debugging skill below says where they live); otherwise ask with `ask_production`. CONFIRM: which services matter most (API, workers, frontend).
- Traces: CONFIRM (e.g. Langfuse project name, if the app uses it).
- Recent deploys: CONFIRM (e.g. the GitHub Actions workflow that deploys, checked with `gh_checks` / `gh_ci_log`).

**Running it locally**
- CONFIRM: the command to start it in the sandbox (e.g. `docker compose -f docker-compose.dev.yml up -d`), the URL and the health check.
- CONFIRM: how to seed test data, and the test account (its values come in as the pack's test secrets).

**Things that often go wrong**
- CONFIRM: known flaky areas, slow tests, environment quirks.
